package appsvc

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"
)

func (s *Server) startSignupPIN(ctx context.Context, state stateMap, pin, otpChannel string) map[string]any {
	pin = strings.TrimSpace(pin)
	if pin == "" {
		return map[string]any{"success": false, "error": "gopay pin missing"}
	}
	refresh := s.ensureAccessToken(ctx, state, s.cfg.TokenRefreshMinTTL, false)
	if !anyBool(refresh["success"]) && !tokenUsable(state, "token", 0) {
		return map[string]any{"success": false, "error": firstNonEmpty(anyString(refresh["error"]), "token refresh failed")}
	}
	phone := firstNonEmpty(stateString(state, "_signup_phone"), stateString(state, "phone"))
	if phone == "" {
		return map[string]any{"success": false, "error": "signup phone missing"}
	}
	client, err := s.newClientWithState(ctx, state, false)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	profile, _ := client.Get(ctx, customerBaseURL+"/v1/users/profile")
	pinSetup := false
	if profile != nil && profile.StatusCode == http.StatusOK {
		pinSetup, _ = pinSetupFlagFromProfileData(profile.Data())
	}
	if pinSetup {
		phone = firstNonEmpty(stringForAnyKey(profile.Data(), "phone", "number"), phone)
		state["phone"] = normalizePhone(phone, "")
		state["stage"] = "ready"
		updatePINSetupState(state, true)
		state["ready_at"] = time.Now().Unix()
		delete(state, "last_error")
		deleteKeys(state, signupAccountStateKeys...)
		deleteKeys(state, signupOTPStateKeys...)
		deleteKeys(state, signupPINStateKeys...)
		return map[string]any{"success": true, "phone": stateString(state, "phone"), "pin_setup_complete": true}
	}
	allowed, err := client.Post(ctx, customerBaseURL+"/api/v1/users/pins/allowed", map[string]any{"pin": pin})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if allowed.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("pin allowed failed", allowed)}
	}
	methods, err := client.Post(ctx, gotoAuthBaseURL+"/cvs/v1/methods", s.authBody(map[string]any{
		"country_code":                 nil,
		"device_verification_token_id": nil,
		"email_address":                nil,
		"flow":                         "goto_pin_wa_sms",
		"phone_number":                 nil,
	}))
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if methods.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("pin otp methods failed", methods)}
	}
	methodsData := methods.Data()
	verificationID := verificationIDFrom(methodsData)
	if verificationID == "" {
		shape := responseShape(methods)
		return map[string]any{"success": false, "error": "pin verification_id missing: " + safeJSON(shape), "response_shape": shape}
	}
	otpMethods := methodsFrom(methodsData)
	method := chooseOTPMethod(otpMethods, otpChannel, "otp_sms")
	if method == "" {
		return map[string]any{"success": false, "error": fmt.Sprintf("otp method unavailable: %v", otpMethods), "response_shape": responseShape(methods)}
	}
	initResp, err := client.Post(ctx, gotoAuthBaseURL+"/cvs/v1/initiate", s.authBody(map[string]any{
		"country_code":                 nil,
		"device_verification_token_id": nil,
		"email_address":                nil,
		"flow":                         "goto_pin_wa_sms",
		"is_multiple_method":           nil,
		"phone_number":                 nil,
		"verification_id":              verificationID,
		"verification_method":          method,
	}))
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if initResp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("pin otp initiate failed", initResp)}
	}
	otpToken := otpTokenFrom(initResp.Data())
	if otpToken == "" {
		return map[string]any{"success": false, "error": "pin otp_token missing"}
	}
	now := time.Now().Unix()
	state["_signup_pin_challenge_id"] = ""
	state["_signup_pin_client_id"] = ""
	state["_signup_pin_verification_id"] = verificationID
	state["_signup_pin_verification_method"] = method
	state["_signup_pin_otp_token"] = otpToken
	state["_signup_pin_otp_sent_at"] = now
	state["_signup_pin_otp_expires_at"] = now + int64(s.cfg.OTPTimeout.Seconds())
	state["stage"] = "signup_pin_otp_pending"
	delete(state, "last_error")
	return map[string]any{"success": true, "otp_sent": true, "verification_id": verificationID, "method": method}
}

func (s *Server) retrySignupPIN(ctx context.Context, state stateMap) map[string]any {
	if stateString(state, "stage") != "signup_pin_otp_pending" {
		return map[string]any{"success": false, "error": fmt.Sprintf("not waiting for signup pin otp: %s", firstNonEmpty(stateString(state, "stage"), "idle"))}
	}
	otpToken := stateString(state, "_signup_pin_otp_token")
	method := firstNonEmpty(stateString(state, "_signup_pin_verification_method"), "otp_sms")
	if otpToken == "" {
		return map[string]any{"success": false, "error": "signup pin otp state missing"}
	}
	client, err := s.newClientWithState(ctx, state, true)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	resp, err := client.Post(ctx, gotoAuthBaseURL+"/cvs/v1/retry", s.authBody(map[string]any{
		"flow":                "goto_pin_wa_sms",
		"verification_method": method,
		"data":                map[string]any{"otp_token": otpToken},
	}))
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if resp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("pin otp retry failed", resp)}
	}
	newToken := otpTokenFrom(resp.Data())
	if newToken == "" {
		return map[string]any{"success": false, "error": "pin retry otp_token missing"}
	}
	now := time.Now().Unix()
	state["_signup_pin_otp_token"] = newToken
	state["_signup_pin_otp_sent_at"] = now
	state["_signup_pin_otp_expires_at"] = now + int64(s.cfg.OTPTimeout.Seconds())
	delete(state, "last_error")
	return map[string]any{"success": true, "otp_sent": true}
}

func (s *Server) completeSignupPIN(ctx context.Context, state stateMap, otp, pin string) map[string]any {
	if stateString(state, "stage") != "signup_pin_otp_pending" {
		return map[string]any{"success": false, "error": fmt.Sprintf("not waiting for signup pin otp: %s", firstNonEmpty(stateString(state, "stage"), "idle"))}
	}
	otp = strings.TrimSpace(otp)
	pin = strings.TrimSpace(pin)
	if otp == "" {
		return map[string]any{"success": false, "error": "signup pin otp required"}
	}
	if pin == "" {
		return map[string]any{"success": false, "error": "gopay pin missing"}
	}
	client, err := s.newClientWithState(ctx, state, true)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	verificationID := stateString(state, "_signup_pin_verification_id")
	method := firstNonEmpty(stateString(state, "_signup_pin_verification_method"), "otp_sms")
	otpToken := stateString(state, "_signup_pin_otp_token")
	if verificationID == "" || otpToken == "" {
		return map[string]any{"success": false, "error": "signup pin otp state missing"}
	}
	verifyResp, err := client.Post(ctx, gotoAuthBaseURL+"/cvs/v1/verify", s.authBody(map[string]any{
		"data":                map[string]any{"otp": otp, "otp_token": otpToken},
		"flow":                "goto_pin_wa_sms",
		"verification_id":     verificationID,
		"verification_method": method,
	}))
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if verifyResp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("pin otp verify failed", verifyResp)}
	}
	verificationToken := verificationTokenFrom(verifyResp.Data())
	if verificationToken == "" {
		return map[string]any{"success": false, "error": "pin verification_token missing"}
	}
	setupResp, err := client.Request(ctx, http.MethodPost, customerBaseURL+"/api/v2/users/pins/setup/tokens", map[string]any{
		"client_id":    stateString(state, "_signup_pin_client_id"),
		"pin":          pin,
		"challenge_id": stateString(state, "_signup_pin_challenge_id"),
	}, http.Header{
		"Verification-Token": []string{"Bearer " + verificationToken},
		"Is-Token-Required":  []string{"false"},
	})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if setupResp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("pin setup failed", setupResp)}
	}
	phone := firstNonEmpty(stateString(state, "_signup_phone"), stateString(state, "phone"))
	state["phone"] = phone
	state["stage"] = "ready"
	updatePINSetupState(state, true)
	state["ready_at"] = time.Now().Unix()
	delete(state, "last_error")
	deleteKeys(state, signupAccountStateKeys...)
	deleteKeys(state, signupOTPStateKeys...)
	deleteKeys(state, signupPINStateKeys...)
	return map[string]any{"success": true, "phone": phone, "pin_setup_complete": true}
}
