package appsvc

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/byte-v-forge/gpt/gopay/protocol"
	gopayapp "github.com/byte-v-forge/gpt/gopay/protocol/app"
)

const loginMethodsMaxAttempts = 3

var (
	loginStateKeys = []string{
		"_login_phone", "_login_country_code", "_login_verification_id",
		"_login_methods", "_login_default_method", "_login_methods_checked_at",
		"_login_flow", "_login_verification_method", "_login_otp_token", "_login_2fa_token",
		"_login_started_at", "_login_otp_sent_at", "_login_otp_expires_at",
	}
	signupAccountStateKeys = []string{"_signup_phone", "_signup_country_code", "_signup_name", "_signup_email"}
	signupOTPStateKeys     = []string{"_signup_verification_id", "_signup_verification_method", "_signup_otp_token", "_signup_started_at", "_signup_otp_sent_at", "_signup_otp_expires_at"}
	signupPINStateKeys     = []string{"_signup_pin_verification_id", "_signup_pin_verification_method", "_signup_pin_otp_token", "_signup_pin_challenge_id", "_signup_pin_client_id", "_signup_pin_otp_sent_at", "_signup_pin_otp_expires_at"}
	activeTokenKeys        = []string{"token", "refresh_token", "token_expires_at"}
	activeTokenMetaKeys    = []string{"last_token_refresh_at", "last_token_refresh_error", "last_token_refresh_failed_at"}
	tmpTokenKeys           = []string{"_tmp_token", "_tmp_refresh_token", "_tmp_token_expires_at"}
	tmpTokenMetaKeys       = []string{"_tmp_phone", "_tmp_token_migrated_at"}
)

func (s *Server) checkPhoneByLoginMethods(ctx context.Context, phone, countryCode string, proxyState stateMap) map[string]any {
	cc := phoneCountryCode(s.cfg, countryCode)
	normalized := normalizePhoneWithConfig(s.cfg, phone, cc)
	if proxyState == nil {
		proxyState = stateMap{}
	}
	proxyURL := stateString(proxyState, "_gopay_proxy")
	if proxyURL == "" {
		return s.checkPhoneResult(proxyState, map[string]any{"success": false, "available": false, "status": "error", "error": "generated proxy missing", "attempts": 0})
	}
	rawDevice := nestedMap(proxyState["device"])
	if len(rawDevice) == 0 {
		return s.checkPhoneResult(proxyState, map[string]any{"success": false, "available": false, "status": "error", "error": "generated device missing", "attempts": 0})
	}
	device := deviceFromMap(rawDevice)
	if device.AppID == "" || device.UniqueID == "" || device.PhoneMake == "" || device.PhoneModel == "" {
		return s.checkPhoneResult(proxyState, map[string]any{"success": false, "available": false, "status": "error", "error": "generated device incomplete", "attempts": 0})
	}
	client, err := s.newClient(ctx, "", proxyURL, device)
	if err != nil {
		return s.checkPhoneResult(proxyState, map[string]any{"success": false, "available": false, "status": "error", "error": err.Error(), "attempts": 0})
	}
	resp, err := client.Post(ctx, gotoAuthBaseURL+"/goto-auth/login/methods", signupProbeBody{
		PhoneNumber:               normalized,
		CountryCode:               cc,
		Email:                     "",
		DeviceVerificationTokenID: "",
		ClientID:                  s.cfg.GotoClientID,
		ClientSecret:              s.cfg.GotoClientSecret,
	})
	if err != nil {
		return s.checkPhoneResult(proxyState, map[string]any{"success": false, "available": false, "status": "error", "error": err.Error(), "attempts": 1})
	}
	if resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusCreated {
		verificationID, methods, defaultMethod := s.persistLoginProbe(proxyState, normalized, cc, resp.Data())
		return s.checkPhoneResult(proxyState, map[string]any{
			"success": true, "available": false, "status": "registered",
			"verification_id_present": verificationID != "", "methods": methods, "default_method": defaultMethod,
			"attempts": 1,
		})
	}
	if loginMethodsInvalidUser(resp) {
		return s.checkPhoneResult(proxyState, map[string]any{"success": true, "available": true, "status": "available", "attempts": 1})
	}
	if isRateLimited(resp) {
		return s.checkPhoneResult(proxyState, map[string]any{"success": false, "available": false, "status": "rate_limited", "error": loginMethodsRateLimitedError(), "attempts": 1})
	}
	return s.checkPhoneResult(proxyState, map[string]any{"success": false, "available": false, "status": "error", "error": apiError("login methods failed", resp), "attempts": 1})
}

func (s *Server) checkPhoneResult(state stateMap, data map[string]any) map[string]any {
	if data == nil {
		data = map[string]any{}
	}
	for key, value := range s.deviceProxyDiagnostics(state) {
		data[key] = value
	}
	data["state_json"] = stateJSON(state)
	return data
}

func (s *Server) startLogin(ctx context.Context, state stateMap, phone, pin, countryCode, otpChannel string) map[string]any {
	cc := phoneCountryCode(s.cfg, countryCode)
	normalized := normalizePhoneWithConfig(s.cfg, phone, cc)
	attempts := loginMethodsMaxAttempts
	var resp *protocol.Response
	var client *gopayapp.Client
	var methods []string
	var defaultMethod string
	var verificationID string
	for attempt := 1; attempt <= attempts; attempt++ {
		if attempt > 1 {
			if err := s.rotateLoginAttemptIdentity(ctx, state); err != nil {
				return map[string]any{"success": false, "error": err.Error()}
			}
		}
		proxyURL, _, _, err := s.proxyForAttempt(attempt, state)
		if err != nil {
			return map[string]any{"success": false, "error": err.Error()}
		}
		device, err := s.ensureDevice(state)
		if err != nil {
			return map[string]any{"success": false, "error": err.Error()}
		}
		state["_login_phone"] = normalized
		state["_login_country_code"] = cc
		state["_login_started_at"] = time.Now().Unix()
		state["stage"] = "login"
		delete(state, "last_error")
		c, err := s.newClient(ctx, "", proxyURL, device)
		if err != nil {
			return map[string]any{"success": false, "error": err.Error()}
		}
		if probeID, probeMethods, probeDefault, ok := s.reusableLoginProbe(state, normalized, cc); ok {
			client = c
			verificationID = probeID
			methods = probeMethods
			defaultMethod = probeDefault
			break
		}
		resp, err = c.Post(ctx, gotoAuthBaseURL+"/goto-auth/login/methods", signupProbeBody{
			PhoneNumber:               normalized,
			CountryCode:               cc,
			Email:                     "",
			DeviceVerificationTokenID: "",
			ClientID:                  s.cfg.GotoClientID,
			ClientSecret:              s.cfg.GotoClientSecret,
		})
		if err != nil {
			if attempt < attempts && retryableGoPayTransportError(err) {
				time.Sleep(loginMethodsBackoff(attempt))
				continue
			}
			return map[string]any{"success": false, "error": err.Error()}
		}
		if resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusCreated {
			client = c
			verificationID, methods, defaultMethod = s.persistLoginProbe(state, normalized, cc, resp.Data())
			break
		}
		if isRateLimited(resp) && attempt < attempts {
			time.Sleep(loginMethodsBackoff(attempt))
			continue
		}
		if isRateLimited(resp) {
			return map[string]any{"success": false, "error": loginMethodsRateLimitedError()}
		}
		if loginMethodsInvalidUser(resp) {
			return map[string]any{"success": false, "not_registered": true, "error": apiError("login methods failed", resp)}
		}
		return map[string]any{"success": false, "error": apiError("login methods failed", resp)}
	}
	if client == nil {
		return map[string]any{"success": false, "error": "login methods failed"}
	}
	if verificationID == "" {
		if resp != nil {
			shape := responseShape(resp)
			return map[string]any{"success": false, "error": "verification_id missing: " + safeJSON(shape), "response_shape": shape}
		}
		return map[string]any{"success": false, "error": "verification_id missing from login probe state"}
	}
	if method := chooseOTPMethod(methods, otpChannel, firstNonEmpty(defaultMethod, "otp_wa")); method != "" {
		otpResp, err := client.Request(ctx, http.MethodPost, gotoAuthBaseURL+"/cvs/v1/initiate", signupInitiateBody{
			VerificationID:            verificationID,
			Flow:                      "login_1fa",
			VerificationMethod:        method,
			CountryCode:               cc,
			EmailAddress:              nil,
			ClientID:                  s.cfg.GotoClientID,
			PhoneNumber:               normalized,
			ClientSecret:              s.cfg.GotoClientSecret,
			IsMultipleMethod:          nil,
			DeviceVerificationTokenID: nil,
		}, nil)
		if err != nil {
			return map[string]any{"success": false, "error": err.Error()}
		}
		if otpResp.StatusCode != http.StatusOK {
			return map[string]any{"success": false, "error": apiError("login otp initiate failed", otpResp)}
		}
		otpToken := otpTokenFrom(otpResp.Data())
		if otpToken == "" {
			return map[string]any{"success": false, "error": "login otp_token missing", "response_shape": responseShape(otpResp)}
		}
		s.persistLoginOTP(state, normalized, cc, verificationID, method, otpToken, "", "login_1fa")
		return map[string]any{"success": true, "ready": false, "otp_sent": true, "verification_id": verificationID, "method": method}
	}
	if !contains(methods, "goto_pin") {
		return map[string]any{"success": false, "error": fmt.Sprintf("goto_pin unavailable: %v", methods)}
	}
	if strings.TrimSpace(pin) == "" {
		return map[string]any{"success": false, "error": "gopay pin missing"}
	}
	c := client
	initResp, err := c.Request(ctx, http.MethodPost, gotoAuthBaseURL+"/cvs/v1/initiate", signupInitiateBody{
		VerificationID:            verificationID,
		Flow:                      "login_1fa",
		VerificationMethod:        "goto_pin",
		CountryCode:               cc,
		EmailAddress:              nil,
		ClientID:                  s.cfg.GotoClientID,
		PhoneNumber:               normalized,
		ClientSecret:              s.cfg.GotoClientSecret,
		IsMultipleMethod:          true,
		DeviceVerificationTokenID: nil,
	}, http.Header{"Authorization": []string{""}})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if initResp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("login pin initiate failed", initResp)}
	}
	challengeID := challengeIDFrom(initResp.Data())
	if challengeID == "" {
		shape := responseShape(initResp)
		return map[string]any{"success": false, "error": "pin challenge_id missing: " + safeJSON(shape), "response_shape": shape}
	}
	if pinPage, err := c.Get(ctx, customerBaseURL+"/api/v2/challenges/"+challengeID+"/pin-page/nb"); err != nil || pinPage.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("pin page failed", pinPage)}
	}
	pinResp, err := c.Post(ctx, customerBaseURL+"/api/v1/users/pin/tokens/nb", map[string]any{
		"challenge_id": challengeID,
		"client_id":    s.cfg.PINClientID,
		"pin":          pin,
	})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if pinResp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("pin token failed", pinResp)}
	}
	validationJWT := stringForAnyKey(pinResp.Data(), "token")
	if validationJWT == "" {
		return map[string]any{"success": false, "error": "pin validation token missing"}
	}
	verifyResp, err := c.Post(ctx, gotoAuthBaseURL+"/cvs/v1/verify", cvsVerifyBody{
		Data:               map[string]any{"challenge_id": challengeID, "validation_jwt": validationJWT},
		Flow:               "login_1fa",
		VerificationID:     verificationID,
		VerificationMethod: "goto_pin",
		ClientID:           s.cfg.GotoClientID,
		ClientSecret:       s.cfg.GotoClientSecret,
	})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if verifyResp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("login pin verify failed", verifyResp)}
	}
	verificationToken := verificationTokenFrom(verifyResp.Data())
	if verificationToken == "" {
		return map[string]any{"success": false, "error": "1fa verification_token missing"}
	}
	accountResp, err := c.Request(ctx, http.MethodPost, gotoAuthBaseURL+"/goto-auth/accountlist", gotoAuthClientBody{
		ClientID:     s.cfg.GotoClientID,
		ClientSecret: s.cfg.GotoClientSecret,
	}, http.Header{"Verification-Token": []string{"Bearer " + verificationToken}})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if accountResp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("accountlist failed", accountResp)}
	}
	accountID := firstAccountID(accountListFrom(accountResp.Data()))
	oneFAToken := oneFATokenFrom(accountResp.Data())
	if accountID == "" || oneFAToken == "" {
		return map[string]any{"success": false, "error": "account_id or 1fa_token missing"}
	}
	tokenResp, err := c.Post(ctx, gotoAuthBaseURL+"/goto-auth/token", gotoCVSTokenBody{
		AccountID:    accountID,
		ExtUserToken: nil,
		GrantType:    "cvs",
		Token:        oneFAToken,
		ClientID:     s.cfg.GotoClientID,
		ClientSecret: s.cfg.GotoClientSecret,
	})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if tokenResp.StatusCode == http.StatusCreated {
		s.persistLoginReady(state, tokenResp.Data(), normalized)
		return map[string]any{"success": true, "ready": true, "otp_sent": false}
	}
	twoFAToken := twoFATokenFrom(tokenResp.Data())
	verificationID = verificationIDFrom(tokenResp.Data())
	if tokenResp.StatusCode != http.StatusForbidden || twoFAToken == "" || verificationID == "" {
		return map[string]any{"success": false, "error": apiError("token exchange failed", tokenResp)}
	}
	otpMethods := methodsFrom(tokenResp.Data())
	method := chooseOTPMethod(otpMethods, otpChannel, "otp_wa")
	if method == "" {
		return map[string]any{"success": false, "error": fmt.Sprintf("otp method unavailable: %v", otpMethods), "response_shape": responseShape(tokenResp)}
	}
	otpResp, err := c.Request(ctx, http.MethodPost, gotoAuthBaseURL+"/cvs/v1/initiate", signupInitiateBody{
		VerificationID:            verificationID,
		Flow:                      "login_2fa",
		VerificationMethod:        method,
		CountryCode:               cc,
		EmailAddress:              nil,
		ClientID:                  s.cfg.GotoClientID,
		PhoneNumber:               normalized,
		ClientSecret:              s.cfg.GotoClientSecret,
		IsMultipleMethod:          nil,
		DeviceVerificationTokenID: nil,
	}, http.Header{"Authorization": []string{""}})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if otpResp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("2fa otp initiate failed", otpResp)}
	}
	otpToken := otpTokenFrom(otpResp.Data())
	if otpToken == "" {
		return map[string]any{"success": false, "error": "2fa otp_token missing"}
	}
	s.persistLoginOTP(state, normalized, cc, verificationID, method, otpToken, twoFAToken, "login_2fa")
	return map[string]any{"success": true, "ready": false, "otp_sent": true, "verification_id": verificationID, "method": method}
}
