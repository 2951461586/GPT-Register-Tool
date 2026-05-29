package appsvc

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/byte-v-forge/gpt/gopay/protocol"
	gopayapp "github.com/byte-v-forge/gpt/gopay/protocol/app"
)

type signupProbeBody struct {
	PhoneNumber               string `json:"phone_number"`
	CountryCode               string `json:"country_code"`
	Email                     string `json:"email"`
	DeviceVerificationTokenID string `json:"device_verification_token_id"`
	ClientID                  string `json:"client_id"`
	ClientSecret              string `json:"client_secret"`
}

type signupMethodsBody struct {
	CountryCode               string `json:"country_code"`
	EmailAddress              any    `json:"email_address"`
	ClientID                  string `json:"client_id"`
	PhoneNumber               string `json:"phone_number"`
	ClientSecret              string `json:"client_secret"`
	Flow                      string `json:"flow"`
	DeviceVerificationTokenID any    `json:"device_verification_token_id"`
}

type signupInitiateBody struct {
	VerificationID            string `json:"verification_id"`
	Flow                      string `json:"flow"`
	VerificationMethod        string `json:"verification_method"`
	CountryCode               string `json:"country_code"`
	EmailAddress              any    `json:"email_address"`
	ClientID                  string `json:"client_id"`
	PhoneNumber               string `json:"phone_number"`
	ClientSecret              string `json:"client_secret"`
	IsMultipleMethod          any    `json:"is_multiple_method"`
	DeviceVerificationTokenID any    `json:"device_verification_token_id"`
}

type cvsVerifyBody struct {
	Data               any    `json:"data"`
	Flow               string `json:"flow"`
	VerificationID     string `json:"verification_id"`
	VerificationMethod string `json:"verification_method"`
	ClientID           string `json:"client_id"`
	ClientSecret       string `json:"client_secret"`
}

type gotoAuthClientBody struct {
	ClientID     string `json:"client_id"`
	ClientSecret string `json:"client_secret"`
}

type gotoCVSTokenBody struct {
	AccountID    string `json:"account_id"`
	ExtUserToken any    `json:"ext_user_token"`
	GrantType    string `json:"grant_type"`
	Token        string `json:"token"`
	ClientID     string `json:"client_id"`
	ClientSecret string `json:"client_secret"`
}

type gotoChallengeTokenBody struct {
	ExtUserToken any    `json:"ext_user_token"`
	GrantType    string `json:"grant_type"`
	Token        string `json:"token"`
	ClientID     string `json:"client_id"`
	ClientSecret string `json:"client_secret"`
}

func (s *Server) completeLogin(ctx context.Context, state stateMap, otp string) error {
	device, err := s.ensureDevice(state)
	if err != nil {
		return err
	}
	client, err := s.newClient(ctx, "", s.proxyForState(state), device)
	if err != nil {
		return err
	}
	verificationID := stateString(state, "_login_verification_id")
	otpToken := stateString(state, "_login_otp_token")
	method := firstNonEmpty(stateString(state, "_login_verification_method"), "otp_wa")
	flow := firstNonEmpty(stateString(state, "_login_flow"), "login_2fa")
	twoFAToken := stateString(state, "_login_2fa_token")
	if verificationID == "" || otpToken == "" {
		return fmt.Errorf("login otp state missing")
	}
	if flow == "login_2fa" && twoFAToken == "" {
		return fmt.Errorf("login 2fa state missing")
	}
	verifyResp, err := client.Post(ctx, gotoAuthBaseURL+"/cvs/v1/verify", cvsVerifyBody{
		Data:               map[string]any{"otp": strings.TrimSpace(otp), "otp_token": otpToken},
		Flow:               flow,
		VerificationID:     verificationID,
		VerificationMethod: method,
		ClientID:           s.cfg.GotoClientID,
		ClientSecret:       s.cfg.GotoClientSecret,
	})
	if err != nil {
		return err
	}
	if verifyResp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s", apiError(flow+" verify failed", verifyResp))
	}
	verificationToken := verificationTokenFrom(verifyResp.Data())
	if verificationToken == "" {
		return fmt.Errorf("%s verification_token missing", flow)
	}
	if flow == "login_1fa" {
		accountResp, err := client.Request(ctx, http.MethodPost, gotoAuthBaseURL+"/goto-auth/accountlist", gotoAuthClientBody{
			ClientID:     s.cfg.GotoClientID,
			ClientSecret: s.cfg.GotoClientSecret,
		}, http.Header{"Verification-Token": []string{"Bearer " + verificationToken}})
		if err != nil {
			return err
		}
		if accountResp.StatusCode != http.StatusOK {
			return fmt.Errorf("%s", apiError("accountlist failed", accountResp))
		}
		accountID := firstAccountID(accountListFrom(accountResp.Data()))
		oneFAToken := oneFATokenFrom(accountResp.Data())
		if accountID == "" || oneFAToken == "" {
			return fmt.Errorf("account_id or 1fa_token missing")
		}
		tokenResp, err := client.Post(ctx, gotoAuthBaseURL+"/goto-auth/token", gotoCVSTokenBody{
			AccountID:    accountID,
			ExtUserToken: nil,
			GrantType:    "cvs",
			Token:        oneFAToken,
			ClientID:     s.cfg.GotoClientID,
			ClientSecret: s.cfg.GotoClientSecret,
		})
		if err != nil {
			return err
		}
		if tokenResp.StatusCode != http.StatusCreated {
			if tokenResp.StatusCode == http.StatusForbidden && twoFATokenFrom(tokenResp.Data()) != "" {
				return s.continueLogin2FA(ctx, client, state, tokenResp)
			}
			return fmt.Errorf("%s", apiError("cvs token failed", tokenResp))
		}
		s.persistLoginReady(state, tokenResp.Data(), stateString(state, "_login_phone"))
		return nil
	}
	tokenResp, err := client.Request(ctx, http.MethodPost, gotoAuthBaseURL+"/goto-auth/token", gotoChallengeTokenBody{
		ExtUserToken: nil,
		GrantType:    "challenge",
		Token:        twoFAToken,
		ClientID:     s.cfg.GotoClientID,
		ClientSecret: s.cfg.GotoClientSecret,
	}, http.Header{"Verification-Token": []string{"Bearer " + verificationToken}})
	if err != nil {
		return err
	}
	if tokenResp.StatusCode != http.StatusCreated {
		return fmt.Errorf("%s", apiError("challenge token failed", tokenResp))
	}
	s.persistLoginReady(state, tokenResp.Data(), stateString(state, "_login_phone"))
	return nil
}

func (s *Server) continueLogin2FA(ctx context.Context, client *gopayapp.Client, state stateMap, tokenResp *protocol.Response) error {
	twoFAToken := twoFATokenFrom(tokenResp.Data())
	verificationID := verificationIDFrom(tokenResp.Data())
	if twoFAToken == "" || verificationID == "" {
		return fmt.Errorf("%s", apiError("cvs token 2fa challenge missing", tokenResp))
	}
	otpMethods := methodsFrom(tokenResp.Data())
	defaultMethod := stringForAnyKey(tokenResp.Data(), "default_method", "defaultMethod")
	previousMethod := stateString(state, "_login_verification_method")
	method := chooseOTPMethod(otpMethods, "", firstNonEmpty(defaultMethod, previousMethod, "otp_wa"))
	if method == "" {
		return fmt.Errorf("2fa otp method unavailable: %v", otpMethods)
	}
	phone := stateString(state, "_login_phone")
	countryCode := stateString(state, "_login_country_code")
	otpResp, err := client.Request(ctx, http.MethodPost, gotoAuthBaseURL+"/cvs/v1/initiate", signupInitiateBody{
		VerificationID:            verificationID,
		Flow:                      "login_2fa",
		VerificationMethod:        method,
		CountryCode:               countryCode,
		EmailAddress:              nil,
		ClientID:                  s.cfg.GotoClientID,
		PhoneNumber:               phone,
		ClientSecret:              s.cfg.GotoClientSecret,
		IsMultipleMethod:          nil,
		DeviceVerificationTokenID: nil,
	}, http.Header{"Authorization": []string{""}})
	if err != nil {
		return err
	}
	if otpResp.StatusCode != http.StatusOK {
		return fmt.Errorf("%s", apiError("2fa otp initiate failed", otpResp))
	}
	otpToken := otpTokenFrom(otpResp.Data())
	if otpToken == "" {
		return fmt.Errorf("2fa otp_token missing")
	}
	s.persistLoginOTP(state, phone, countryCode, verificationID, method, otpToken, twoFAToken, "login_2fa")
	return nil
}

func (s *Server) startSignup(ctx context.Context, state stateMap, phone, name, email, countryCode, otpChannel string, skipPhoneProbe bool) map[string]any {
	cc := phoneCountryCode(s.cfg, countryCode)
	normalized := normalizePhoneWithConfig(s.cfg, phone, cc)
	if normalized == "" {
		return map[string]any{"success": false, "error": "signup phone missing"}
	}
	name, email = s.signupProfile(normalized, name, email)
	if name == "" {
		return map[string]any{"success": false, "error": "signup name missing"}
	}
	if cooldown := s.signupCooldownResult(state); cooldown != nil {
		return cooldown
	}
	s.clearSignupState(state, "")
	s.clearLoginState(state, "")
	device, err := s.ensureDevice(state)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if strings.TrimSpace(os.Getenv("GOPAY_APP_VERSION")) == "" && !strings.HasPrefix(strings.TrimSpace(device.AppVersion), "2.7.") {
		next, rawDevice, err := s.newLogonDevice()
		if err != nil {
			return map[string]any{"success": false, "error": err.Error()}
		}
		device = next
		state["device"] = rawDevice
	}
	if probeTransactionID := stateString(state, "_signup_probe_transaction_id"); probeTransactionID != "" {
		device.TransactionID = probeTransactionID
	} else {
		state["_signup_probe_transaction_id"] = device.TransactionID
	}
	state["device"] = deviceToMap(device)
	deleteKeys(state, activeTokenKeys...)
	deleteKeys(state, activeTokenMetaKeys...)
	deleteKeys(state, tmpTokenKeys...)
	deleteKeys(state, tmpTokenMetaKeys...)
	state["_signup_phone"] = normalized
	state["_signup_country_code"] = cc
	state["_signup_name"] = name
	state["_signup_email"] = email
	state["_signup_started_at"] = time.Now().Unix()
	state["_signup_skip_phone_probe"] = skipPhoneProbe
	state["stage"] = "signup"
	delete(state, "last_error")
	proxyURL := s.proxyForState(state)
	client, err := s.newClient(ctx, "", proxyURL, device)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	supportWarmup := map[string]any{"attempted": true}
	if warmupResp, err := client.InitiateSupportCustomer(ctx); err != nil {
		supportWarmup["success"] = false
		supportWarmup["error"] = err.Error()
		state["_signup_support_warmup_error"] = err.Error()
	} else {
		status := 0
		if warmupResp != nil {
			status = warmupResp.StatusCode
		}
		supportWarmup["success"] = status >= 200 && status < 300
		supportWarmup["status_code"] = status
		state["_signup_support_warmup_status"] = status
		delete(state, "_signup_support_warmup_error")
	}
	state["_signup_support_warmup_at"] = time.Now().Unix()
	if skipPhoneProbe {
		state["_signup_phone_probe_skipped"] = true
		supportWarmup["phone_probe_skipped"] = true
	} else {
		probeResp, err := client.Post(ctx, gotoAuthBaseURL+"/goto-auth/login/methods", signupProbeBody{
			PhoneNumber:               normalized,
			CountryCode:               cc,
			Email:                     "",
			DeviceVerificationTokenID: "",
			ClientID:                  s.cfg.GotoClientID,
			ClientSecret:              s.cfg.GotoClientSecret,
		})
		if err != nil {
			return map[string]any{"success": false, "error": err.Error()}
		}
		if probeResp.StatusCode == http.StatusOK || probeResp.StatusCode == http.StatusCreated {
			return map[string]any{"success": false, "error": "PHONE_REGISTERED", "raw_json": safeJSON(probeResp.Payload)}
		}
		if isRateLimited(probeResp) {
			return s.signupRateLimitResult(state, signupRateLimitScopeProbe, normalized, cc, rateLimitLabel(signupRateLimitScopeProbe), probeResp)
		}
		if !loginMethodsInvalidUser(probeResp) && probeResp.StatusCode >= http.StatusBadRequest {
			return map[string]any{"success": false, "error": apiError("signup phone probe failed", probeResp), "support_warmup": supportWarmup, "raw_json": safeJSON(probeResp.Payload)}
		}
	}
	device = device.WithNewTransactionID()
	state["_signup_cvs_transaction_id"] = device.TransactionID
	state["device"] = deviceToMap(device)
	client, err = s.newClient(ctx, "", proxyURL, device)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error(), "support_warmup": supportWarmup}
	}
	methodsResp, err := client.Post(ctx, gotoAuthBaseURL+"/cvs/v1/methods", signupMethodsBody{
		CountryCode:               cc,
		DeviceVerificationTokenID: nil,
		EmailAddress:              nil,
		Flow:                      "signup",
		PhoneNumber:               normalized,
		ClientID:                  s.cfg.GotoClientID,
		ClientSecret:              s.cfg.GotoClientSecret,
	})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if isRateLimited(methodsResp) {
		return s.signupRateLimitResult(state, signupRateLimitScopeMethods, normalized, cc, rateLimitLabel(signupRateLimitScopeMethods), methodsResp)
	}
	if methodsResp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("signup methods failed", methodsResp), "raw_json": safeJSON(methodsResp.Payload)}
	}
	methodsData := methodsResp.Data()
	verificationID := verificationIDFrom(methodsData)
	if verificationID == "" {
		shape := responseShape(methodsResp)
		return map[string]any{"success": false, "error": "signup verification_id missing: " + safeJSON(shape), "response_shape": shape}
	}
	methods := methodsFrom(methodsData)
	defaultMethod := stringForAnyKey(methodsData, "default_method", "defaultMethod")
	method := chooseOTPMethod(methods, otpChannel, firstNonEmpty(defaultMethod, "otp_wa"))
	if method == "" {
		return map[string]any{"success": false, "error": fmt.Sprintf("otp method unavailable: %v", methods), "response_shape": responseShape(methodsResp)}
	}
	initiateDelay := s.signupInitiateDelay()
	if initiateDelay > 0 {
		now := time.Now().Unix()
		state["_signup_initiate_delay_seconds"] = int64(initiateDelay.Seconds())
		state["_signup_initiate_delay_started_at"] = now
		if err := sleepWithContext(ctx, initiateDelay); err != nil {
			return map[string]any{"success": false, "error": err.Error(), "signup_initiate_delay_seconds": int64(initiateDelay.Seconds())}
		}
		state["_signup_initiate_delay_finished_at"] = time.Now().Unix()
	}
	initResp, err := client.Request(ctx, http.MethodPost, gotoAuthBaseURL+"/cvs/v1/initiate", signupInitiateBody{
		CountryCode:               cc,
		DeviceVerificationTokenID: nil,
		EmailAddress:              nil,
		Flow:                      "signup",
		IsMultipleMethod:          nil,
		PhoneNumber:               normalized,
		VerificationID:            verificationID,
		VerificationMethod:        method,
		ClientID:                  s.cfg.GotoClientID,
		ClientSecret:              s.cfg.GotoClientSecret,
	}, nil)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if isRateLimited(initResp) {
		return s.signupRateLimitResult(state, signupRateLimitScopeInitiate, normalized, cc, rateLimitLabel(signupRateLimitScopeInitiate), initResp)
	}
	if initResp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("signup otp initiate failed", initResp), "method": method, "raw_json": safeJSON(initResp.Payload)}
	}
	otpToken := otpTokenFrom(initResp.Data())
	if otpToken == "" {
		return map[string]any{"success": false, "error": "signup otp_token missing", "raw_json": safeJSON(initResp.Payload)}
	}
	s.persistSignupOTP(state, verificationID, method, otpToken)
	return map[string]any{
		"success": true, "otp_sent": true, "verification_id": verificationID,
		"method": method, "default_method": defaultMethod, "retry_timer_seconds": initResp.Data()["retry_timer_in_seconds"],
		"signup_initiate_delay_seconds": int64(initiateDelay.Seconds()),
		"support_warmup":                supportWarmup,
		"raw_json":                      safeJSON(initResp.Payload),
	}
}

func (s *Server) retrySignupOTP(ctx context.Context, state stateMap) map[string]any {
	if stateString(state, "stage") != "signup_otp_pending" {
		return map[string]any{"success": false, "error": fmt.Sprintf("not waiting for signup otp: %s", firstNonEmpty(stateString(state, "stage"), "idle"))}
	}
	otpToken := stateString(state, "_signup_otp_token")
	method := firstNonEmpty(stateString(state, "_signup_verification_method"), "otp_sms")
	if otpToken == "" {
		return map[string]any{"success": false, "error": "signup otp state missing"}
	}
	device, err := s.ensureDevice(state)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	client, err := s.newClient(ctx, "", s.proxyForState(state), device)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	resp, err := client.Post(ctx, gotoAuthBaseURL+"/cvs/v1/retry", s.authBody(map[string]any{
		"flow":                "signup",
		"verification_method": method,
		"data":                map[string]any{"otp_token": otpToken},
	}))
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if resp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("signup otp retry failed", resp), "raw_json": safeJSON(resp.Payload)}
	}
	if newToken := otpTokenFrom(resp.Data()); newToken != "" {
		state["_signup_otp_token"] = newToken
	}
	now := time.Now().Unix()
	state["_signup_otp_sent_at"] = now
	state["_signup_otp_expires_at"] = now + int64(s.cfg.OTPTimeout.Seconds())
	state["stage"] = "signup_otp_pending"
	delete(state, "last_error")
	return map[string]any{"success": true, "otp_sent": true, "raw_json": safeJSON(resp.Payload)}
}

func (s *Server) completeSignup(ctx context.Context, state stateMap, otp string) map[string]any {
	if stateString(state, "stage") != "signup_otp_pending" {
		return map[string]any{"success": false, "error": fmt.Sprintf("not waiting for signup otp: %s", firstNonEmpty(stateString(state, "stage"), "idle"))}
	}
	otp = strings.TrimSpace(otp)
	if otp == "" {
		return map[string]any{"success": false, "error": "signup otp required"}
	}
	phone := stateString(state, "_signup_phone")
	cc := firstNonEmpty(stateString(state, "_signup_country_code"), phoneCountryCode(s.cfg, ""))
	name := stateString(state, "_signup_name")
	email := stateString(state, "_signup_email")
	verificationID := stateString(state, "_signup_verification_id")
	method := firstNonEmpty(stateString(state, "_signup_verification_method"), "otp_sms")
	otpToken := stateString(state, "_signup_otp_token")
	if phone == "" || verificationID == "" || otpToken == "" {
		return map[string]any{"success": false, "error": "signup otp state missing"}
	}
	device, err := s.ensureDevice(state)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	client, err := s.newClient(ctx, "", s.proxyForState(state), device)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	verifyResp, err := client.Post(ctx, gotoAuthBaseURL+"/cvs/v1/verify", s.authBody(map[string]any{
		"data":                map[string]any{"otp": otp, "otp_token": otpToken},
		"flow":                "signup",
		"verification_id":     verificationID,
		"verification_method": method,
	}))
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if verifyResp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("signup otp verify failed", verifyResp), "raw_json": safeJSON(verifyResp.Payload)}
	}
	verificationToken := verificationTokenFrom(verifyResp.Data())
	if verificationToken == "" {
		return map[string]any{"success": false, "error": "signup verification_token missing", "raw_json": safeJSON(verifyResp.Payload)}
	}
	signupResp, err := client.Request(ctx, http.MethodPost, gojekBaseURL+"/v7/customers/signup", map[string]any{
		"client_name":   s.cfg.GotoClientID,
		"client_secret": s.cfg.GotoClientSecret,
		"data": map[string]any{
			"name":               name,
			"phone":              cc + phone,
			"email":              email,
			"signed_up_country":  cc,
			"onboarding_partner": "gopay_consumer_app",
		},
	}, http.Header{
		"Authorization":      []string{s.signupBasicAuthorization()},
		"Verification-Token": []string{"Bearer " + verificationToken},
	})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if signupResp.StatusCode != http.StatusCreated {
		return map[string]any{"success": false, "error": apiError("customer signup failed", signupResp), "raw_json": safeJSON(signupResp.Payload)}
	}
	s.storeTokenResponse(state, signupResp.Data(), false)
	state["phone"] = phone
	state["name"] = name
	state["email"] = email
	state["stage"] = "signup_pin_required"
	delete(state, "last_error")
	deleteKeys(state, signupOTPStateKeys...)
	refresh := s.ensureAccessToken(ctx, state, 0, true)
	if !anyBool(refresh["success"]) {
		state["last_error"] = anyString(refresh["error"])
		return map[string]any{"success": false, "error": stateString(state, "last_error"), "raw_json": safeJSON(signupResp.Payload)}
	}
	state["stage"] = "signup_pin_required"
	return map[string]any{"success": true, "phone": phone, "pin_setup_required": true, "raw_json": safeJSON(signupResp.Payload)}
}
