package appsvc

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/byte-v-forge/gpt/gopay/protocol"
)

type anyClient interface {
	Get(context.Context, string, ...int) (*protocol.Response, error)
}

func (s *Server) changePhoneStart(ctx context.Context, state stateMap, pin, newPhone, countryCode string) map[string]any {
	pin = strings.TrimSpace(pin)
	cc := phoneCountryCode(s.cfg, countryCode)
	phone := normalizePhoneWithConfig(s.cfg, newPhone, cc)
	if !tokenUsable(state, "token", 30*time.Second) {
		return map[string]any{"success": false, "error": "account token missing"}
	}
	if pin == "" {
		return map[string]any{"success": false, "error": "gopay pin missing"}
	}
	if phone == "" {
		return map[string]any{"success": false, "error": "new_phone required"}
	}
	if stateInt(state, "_temp_phone_usage_"+phone) >= 2 {
		return map[string]any{"success": false, "error": "PHONE_EXHAUSTED"}
	}
	client, err := s.clientForState(ctx, state)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	profile, profileErr := s.loadGojekProfile(ctx, client)
	if profileErr == "" {
		s.syncProfileFields(state, profile, cc)
	} else if stateString(state, "email") == "" {
		return map[string]any{"success": false, "error": profileErr}
	}
	body := s.changePhoneProfileBody(state, cc, phone)
	checked := stateString(state, "_checked_change_phone") == phone && stateString(state, "_checked_change_phone_status") == "available"
	if !checked {
		resp, err := client.Patch(ctx, gojekBaseURL+"/v5/customers", body)
		if err != nil {
			return map[string]any{"success": false, "error": err.Error()}
		}
		if phoneRegisteredResponse(resp) {
			return map[string]any{"success": false, "error": "PHONE_REGISTERED"}
		}
		if otpTokenFrom(resp.Data()) != "" && resp.StatusCode == http.StatusOK {
			return storeChangePhoneOTPState(state, phone, resp.Data())
		}
		if resp.StatusCode != 461 {
			return map[string]any{"success": false, "error": apiError("pin challenge failed", resp)}
		}
	}
	resp, err := client.Request(ctx, http.MethodPatch, gojekBaseURL+"/v5/customers", body, http.Header{"pin": []string{pin}})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if phoneRegisteredResponse(resp) {
		return map[string]any{"success": false, "error": "PHONE_REGISTERED"}
	}
	if resp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("pin submit failed", resp)}
	}
	return storeChangePhoneOTPState(state, phone, resp.Data())
}

func storeChangePhoneOTPState(state stateMap, phone string, data any) map[string]any {
	otpToken := otpTokenFrom(data)
	if otpToken == "" {
		return map[string]any{"success": false, "error": "otp_token missing"}
	}
	now := time.Now().Unix()
	state["_change_phone"] = phone
	state["_change_otp_token"] = otpToken
	state["_change_otp_sent_at"] = now
	state["_change_otp_expires_at"] = now + firstNonZero(intForAnyKey(data, "expires_in", "otp_expires_in"), 300)
	state["stage"] = "change_phone_otp_pending"
	deleteKeys(state, "_checked_change_phone", "_checked_change_phone_status", "last_error")
	return map[string]any{"success": true, "new_phone": phone, "otp_sent": true}
}

func (s *Server) changePhoneRetry(ctx context.Context, state stateMap) map[string]any {
	otpToken := stateString(state, "_change_otp_token")
	phone := stateString(state, "_change_phone")
	if otpToken == "" || phone == "" {
		return map[string]any{"success": false, "error": fmt.Sprintf("not waiting for change phone otp: %s", firstNonEmpty(stateString(state, "stage"), "idle"))}
	}
	client, err := s.clientForState(ctx, state)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	resp, err := client.Post(ctx, gojekBaseURL+"/v2/otp/retry", map[string]any{"otp_token": otpToken, "channel_type": "sms"})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if resp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("otp retry failed", resp)}
	}
	newToken := otpTokenFrom(resp.Data())
	if newToken == "" {
		return map[string]any{"success": false, "error": "retry otp_token missing"}
	}
	now := time.Now().Unix()
	state["_change_otp_token"] = newToken
	state["_change_otp_sent_at"] = now
	state["_change_otp_expires_at"] = now + firstNonZero(intForAnyKey(resp.Data(), "otp_expires_in", "expires_in"), 300)
	state["stage"] = "change_phone_otp_pending"
	delete(state, "last_error")
	return map[string]any{"success": true, "otp_sent": true}
}

func (s *Server) changePhoneComplete(ctx context.Context, state stateMap, otp string) map[string]any {
	otpToken := stateString(state, "_change_otp_token")
	phone := stateString(state, "_change_phone")
	if otpToken == "" || phone == "" {
		return map[string]any{"success": false, "error": fmt.Sprintf("not waiting for change phone otp: %s", firstNonEmpty(stateString(state, "stage"), "idle"))}
	}
	if strings.TrimSpace(otp) == "" {
		return map[string]any{"success": false, "error": "otp required"}
	}
	client, err := s.clientForState(ctx, state)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	resp, err := client.Post(ctx, gojekBaseURL+"/v5/customers/verificationUpdateProfile", map[string]any{"otp": strings.TrimSpace(otp), "otp_token": otpToken})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if resp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("otp verify failed", resp)}
	}
	if ok, errMsg := s.confirmChangePhone(ctx, client, "", phone); !ok {
		state["last_error"] = errMsg
		return map[string]any{"success": false, "error": errMsg}
	}
	state["phone"] = phone
	state["_temp_phone_usage_"+phone] = stateInt(state, "_temp_phone_usage_"+phone) + 1
	s.migrateActiveTokensToTmp(state, phone)
	state["stage"] = "phone_changed"
	deleteKeys(state, "last_error", "_change_phone", "_change_otp_token", "_change_otp_sent_at", "_change_otp_expires_at")
	return map[string]any{"success": true}
}

func (s *Server) loadGojekProfile(ctx context.Context, client anyClient) (map[string]any, string) {
	resp, err := client.Get(ctx, gojekBaseURL+"/gojek/v2/customer")
	if err != nil {
		return nil, err.Error()
	}
	if resp.StatusCode != http.StatusOK {
		return nil, apiError("customer profile failed", resp)
	}
	for _, candidate := range []any{resp.Data(), resp.Payload} {
		if profile := gojekCustomerProfile(candidate); len(profile) > 0 {
			return profile, ""
		}
	}
	return nil, "customer profile missing"
}

func gojekCustomerProfile(value any) map[string]any {
	obj, ok := jsonObject(value)
	if !ok {
		return nil
	}
	for _, key := range []string{"customer", "profile", "user"} {
		if profile := nestedMap(obj[key]); len(profile) > 0 {
			return profile
		}
	}
	for _, key := range []string{"data", "raw"} {
		if profile := gojekCustomerProfile(obj[key]); len(profile) > 0 {
			return profile
		}
	}
	if looksLikeProfile(obj) {
		return obj
	}
	return nil
}

func looksLikeProfile(value map[string]any) bool {
	for _, key := range []string{"name", "email", "phone", "number", "profile_image_url", "profileImageUrl"} {
		if anyString(value[key]) != "" {
			return true
		}
	}
	return false
}

func (s *Server) syncProfileFields(state stateMap, profile map[string]any, countryCode string) {
	if name := anyString(profile["name"]); name != "" {
		state["name"] = name
	}
	if email := anyString(profile["email"]); email != "" {
		state["email"] = email
	}
	if phone := firstNonEmpty(anyString(profile["phone"]), anyString(profile["number"])); phone != "" {
		state["phone"] = normalizePhone(phone, countryCode)
	}
}

func (s *Server) changePhoneProfileBody(state stateMap, countryCode, phone string) map[string]any {
	name := firstNonEmpty(stateString(state, "name"), "gg")
	email := stateString(state, "email")
	if email == "" {
		email = fallbackProfileEmail(countryCode, phone)
	}
	state["name"] = name
	state["email"] = email
	return map[string]any{"email": email, "name": name, "phone": countryCode + phone, "profile_image_url": nil}
}

func fallbackProfileEmail(countryCode, phone string) string {
	digits := digitsRE.ReplaceAllString(countryCode+phone, "")
	if digits == "" {
		digits = fmt.Sprint(time.Now().Unix())
	}
	return "gopay" + digits + "@gmail.com"
}

func (s *Server) confirmChangePhone(ctx context.Context, client anyClient, countryCode, expectedPhone string) (bool, string) {
	expected := normalizePhone(expectedPhone, countryCode)
	deadline := time.Now().Add(s.cfg.ChangePhoneConfirmTimeout)
	last := ""
	for {
		profile, errMsg := s.loadGojekProfile(ctx, client)
		if errMsg != "" {
			last = errMsg
		} else {
			actual := normalizePhone(firstNonEmpty(anyString(profile["phone"]), anyString(profile["number"])), countryCode)
			if actual == expected {
				return true, ""
			}
			last = fmt.Sprintf("phone change not confirmed: expected %s, got %s", expected, firstNonEmpty(actual, "-"))
		}
		if time.Now().After(deadline) {
			return false, last
		}
		time.Sleep(s.cfg.ChangePhoneConfirmInterval)
	}
}

func phoneRegisteredResponse(resp *protocol.Response) bool {
	if resp == nil || resp.StatusCode != http.StatusBadRequest {
		return false
	}
	text := strings.ToLower(responseText(resp))
	return strings.Contains(text, "user_can_not_update_phone") || strings.Contains(text, "already registered")
}
