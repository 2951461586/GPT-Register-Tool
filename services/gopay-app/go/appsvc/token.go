package appsvc

import (
	"context"
	"fmt"
	"net/http"
	"time"

	"github.com/byte-v-forge/gpt/gopay/protocol"
	gopayapp "github.com/byte-v-forge/gpt/gopay/protocol/app"
)

func (s *Server) newClientWithState(ctx context.Context, state stateMap, requireToken bool) (*gopayapp.Client, error) {
	if requireToken {
		refresh := s.ensureAccessToken(ctx, state, s.cfg.TokenRefreshMinTTL, false)
		if !anyBool(refresh["success"]) && !tokenUsable(state, "token", 0) {
			return nil, fmt.Errorf("%s", firstNonEmpty(anyString(refresh["error"]), "token refresh failed"))
		}
	}
	device, err := s.ensureDevice(state)
	if err != nil {
		return nil, err
	}
	return s.newClient(ctx, stateString(state, "token"), s.proxyForState(state), device)
}

func (s *Server) storeTokenResponse(state stateMap, data map[string]any, preserveRefresh bool) {
	token := protocol.StringAt(data, "access_token")
	if token == "" {
		return
	}
	state["token"] = token
	refresh := protocol.StringAt(data, "refresh_token")
	if refresh != "" {
		state["refresh_token"] = refresh
	} else if !preserveRefresh {
		delete(state, "refresh_token")
	}
	expiresAt := jwtExpiresAt(token)
	if expiresAt == 0 {
		expiresIn := anyInt(data["expires_in"])
		if expiresIn > 0 {
			expiresAt = time.Now().Unix() + expiresIn
		}
	}
	if expiresAt > 0 {
		state["token_expires_at"] = expiresAt
	} else {
		delete(state, "token_expires_at")
	}
	deleteKeys(state, "last_token_refresh_error", "last_token_refresh_failed_at")
}

func (s *Server) refreshAccessToken(ctx context.Context, state stateMap) map[string]any {
	refreshToken := stateString(state, "refresh_token")
	if refreshToken == "" {
		return map[string]any{"success": false, "error": "refresh_token missing"}
	}
	device, err := s.ensureDevice(state)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	client, err := s.newClient(ctx, stateString(state, "token"), s.proxyForState(state), device)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	var last *protocol.Response
	for _, body := range []map[string]any{
		s.authBody(map[string]any{"grant_type": "refresh_token", "token": refreshToken}),
		s.authBody(map[string]any{"grant_type": "refresh_token", "refresh_token": refreshToken}),
	} {
		resp, err := client.Post(ctx, gotoAuthBaseURL+"/goto-auth/token", body)
		if err != nil {
			state["last_token_refresh_error"] = err.Error()
			continue
		}
		last = resp
		if (resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusCreated) && protocol.StringAt(resp.Data(), "access_token") != "" {
			s.storeTokenResponse(state, resp.Data(), true)
			state["last_token_refresh_at"] = time.Now().Unix()
			deleteKeys(state, "last_token_refresh_error", "last_token_refresh_failed_at")
			if stateString(state, "last_error") == "TOKEN_REFRESH_FAILED" {
				delete(state, "last_error")
			}
			return map[string]any{"success": true, "refreshed": true, "expires_at": stateInt(state, "token_expires_at")}
		}
	}
	errMessage := apiError("refresh token failed", last)
	state["last_token_refresh_error"] = errMessage
	state["last_token_refresh_failed_at"] = time.Now().Unix()
	if !tokenUsable(state, "token", 0) {
		state["last_error"] = "TOKEN_REFRESH_FAILED"
	}
	return map[string]any{"success": false, "error": errMessage}
}

func (s *Server) ensureAccessToken(ctx context.Context, state stateMap, minTTL time.Duration, force bool) map[string]any {
	token := stateString(state, "token")
	expiresAt := jwtExpiresAt(token)
	if expiresAt > 0 {
		state["token_expires_at"] = expiresAt
	}
	if token != "" && !force && tokenUsable(state, "token", minTTL) {
		return map[string]any{"success": true, "refreshed": false, "expires_at": expiresAt}
	}
	result := s.refreshAccessToken(ctx, state)
	if anyBool(result["success"]) {
		return result
	}
	if token != "" && tokenUsable(state, "token", 0) {
		return map[string]any{"success": true, "refreshed": false, "expires_at": expiresAt, "warning": result["error"]}
	}
	return result
}

func (s *Server) verifyAccessToken(ctx context.Context, state stateMap) map[string]any {
	token := stateString(state, "token")
	if token == "" {
		return map[string]any{"success": false, "error": "access_token missing", "status": 0}
	}
	client, err := s.newClientWithState(ctx, state, false)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error(), "status": 0}
	}
	resp, err := client.Get(ctx, customerBaseURL+"/v1/users/profile")
	if err != nil {
		return map[string]any{"success": false, "error": err.Error(), "status": 0}
	}
	if resp.StatusCode == http.StatusOK {
		data := resp.Data()
		if profile := gojekCustomerProfile(data); len(profile) > 0 {
			s.syncProfileFields(state, profile, "")
		} else {
			phone := firstNonEmpty(protocol.StringAt(data, "phone"), protocol.StringAt(data, "number"))
			if phone != "" {
				state["phone"] = normalizePhone(phone, "")
			}
		}
		pinSetup, pinSetupKnown := pinSetupFlagFromProfileData(data)
		if pinSetupKnown {
			updatePINSetupState(state, pinSetup)
		}
		state["stage"] = "ready"
		state["ready_at"] = time.Now().Unix()
		delete(state, "last_error")
		return map[string]any{"success": true, "status": 200, "phone": stateString(state, "phone"), "pin_setup": pinSetupKnown && pinSetup}
	}
	return map[string]any{"success": false, "status": resp.StatusCode, "error": apiError("profile failed", resp)}
}

func (s *Server) refreshPINSetupFromProfile(ctx context.Context, client anyClient, state stateMap) (bool, bool, string) {
	resp, err := client.Get(ctx, customerBaseURL+"/v1/users/profile")
	if err != nil {
		return false, false, err.Error()
	}
	if resp.StatusCode != http.StatusOK {
		return false, false, apiError("pin setup check failed", resp)
	}
	pinSetup, ok := pinSetupFlagFromProfileData(resp.Data())
	if !ok {
		return false, false, "is_pin_setup missing"
	}
	updatePINSetupState(state, pinSetup)
	return pinSetup, true, ""
}

func updatePINSetupState(state stateMap, pinSetup bool) {
	now := time.Now().Unix()
	state["pin_setup"] = pinSetup
	state["pin_setup_checked_at"] = now
	if pinSetup {
		state["pin_setup_at"] = now
		return
	}
	delete(state, "pin_setup_at")
}

func pinSetupFlagFromProfileData(value any) (bool, bool) {
	wanted := map[string]struct{}{
		normalizeJSONKey("is_pin_setup"): {},
		normalizeJSONKey("isPinSetup"):   {},
	}
	var walk func(any) (bool, bool)
	walk = func(current any) (bool, bool) {
		if obj, ok := jsonObject(current); ok {
			for key, item := range obj {
				if _, ok := wanted[normalizeJSONKey(key)]; ok {
					return anyBool(item), true
				}
			}
			for _, item := range obj {
				if value, ok := walk(item); ok {
					return value, true
				}
			}
			return false, false
		}
		if items, ok := current.([]any); ok {
			for _, item := range items {
				if value, ok := walk(item); ok {
					return value, true
				}
			}
		}
		return false, false
	}
	return walk(value)
}

func (s *Server) checkTokenValid(ctx context.Context, state stateMap) map[string]any {
	profile := s.verifyAccessToken(ctx, state)
	if anyBool(profile["success"]) {
		return s.tokenValidResult(ctx, state, profile, false)
	}
	refresh := s.refreshAccessToken(ctx, state)
	if !anyBool(refresh["success"]) {
		return map[string]any{"success": false, "token_valid": false, "refreshed": false, "error": firstNonEmpty(anyString(refresh["error"]), anyString(profile["error"]), "token invalid")}
	}
	profile = s.verifyAccessToken(ctx, state)
	if anyBool(profile["success"]) {
		return s.tokenValidResult(ctx, state, profile, true)
	}
	return map[string]any{"success": false, "token_valid": false, "refreshed": true, "error": firstNonEmpty(anyString(profile["error"]), "profile failed after refresh")}
}

func (s *Server) tokenValidResult(ctx context.Context, state stateMap, profile map[string]any, refreshed bool) map[string]any {
	balance := s.checkBalance(ctx, state)
	balanceOK := anyBool(balance["success"])
	amount := anyInt(balance["balance_amount"])
	currency := anyString(balance["balance_currency"])
	if !balanceOK {
		amount = firstNonZero(amount, stateInt(state, "balance_amount"))
		currency = firstNonEmpty(currency, stateString(state, "balance_currency"))
	}
	cachedMinBalance := !balanceOK && (anyBool(state["has_min_balance"]) || stateInt(state, "balance_amount") >= s.cfg.MinBalanceRp)
	hasMinBalance := anyBool(balance["has_min_balance"]) || cachedMinBalance
	result := map[string]any{
		"success":          balanceOK || cachedMinBalance,
		"token_valid":      true,
		"refreshed":        refreshed,
		"phone":            profile["phone"],
		"balance_amount":   amount,
		"balance_currency": firstNonEmpty(currency, "IDR"),
		"has_min_balance":  hasMinBalance,
	}
	if cachedMinBalance {
		result["cached_balance"] = true
		result["balance_check_error"] = firstNonEmpty(anyString(balance["error"]), "balance check failed")
	}
	if !balanceOK && !cachedMinBalance {
		result["error"] = firstNonEmpty(anyString(balance["error"]), "balance check failed")
	}
	return result
}

func (s *Server) checkBalance(ctx context.Context, state stateMap) map[string]any {
	if stateString(state, "token") == "" {
		return map[string]any{"success": false, "error": "access_token missing", "status": 0}
	}
	client, err := s.newClientWithState(ctx, state, false)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error(), "status": 0}
	}
	resp, err := client.Get(ctx, customerBaseURL+"/v1/payment-options/balances")
	state["last_balance_check_at"] = time.Now().Unix()
	if err != nil {
		state["last_balance_error"] = err.Error()
		return map[string]any{"success": false, "status": 0, "error": err.Error()}
	}
	if resp.StatusCode != http.StatusOK {
		errMessage := apiError("balance check failed", resp)
		state["last_balance_error"] = errMessage
		return map[string]any{"success": false, "status": resp.StatusCode, "error": errMessage}
	}
	if resp.Payload["success"] == false {
		errMessage := apiError("balance check failed", resp)
		state["last_balance_error"] = errMessage
		return map[string]any{"success": false, "status": resp.StatusCode, "error": errMessage}
	}
	amount, currency := walletBalance(resp.Payload["data"])
	if amount < 0 {
		errMessage := "gopay wallet balance missing"
		state["last_balance_error"] = errMessage
		return map[string]any{"success": false, "status": resp.StatusCode, "error": errMessage}
	}
	hasMin := amount >= s.cfg.MinBalanceRp
	state["balance_amount"] = amount
	state["balance_currency"] = firstNonEmpty(currency, "IDR")
	state["has_min_balance"] = hasMin
	delete(state, "last_balance_error")
	if hasMin {
		if stateString(state, "last_error") == "INSUFFICIENT_GOPAY_BALANCE" {
			delete(state, "last_error")
		}
	} else {
		state["last_error"] = "INSUFFICIENT_GOPAY_BALANCE"
	}
	return map[string]any{"success": true, "status": 200, "balance_amount": amount, "balance_currency": stateString(state, "balance_currency"), "has_min_balance": hasMin}
}

func tmpTokenUsable(state stateMap, minTTL time.Duration) bool {
	token := stateString(state, "_tmp_token")
	if token == "" {
		return false
	}
	expiresAt := firstNonZero(jwtExpiresAt(token), stateInt(state, "_tmp_token_expires_at"))
	if expiresAt == 0 {
		return true
	}
	return expiresAt > time.Now().Add(minTTL).Unix()
}

func (s *Server) migrateActiveTokensToTmp(state stateMap, phone string) bool {
	moved := false
	for _, key := range activeTokenKeys {
		if value, ok := state[key]; ok && anyString(value) != "" {
			state["_tmp_"+key] = value
			moved = true
		}
		delete(state, key)
	}
	for _, key := range activeTokenMetaKeys {
		delete(state, key)
	}
	if moved {
		state["_tmp_token_migrated_at"] = time.Now().Unix()
		if phone != "" {
			state["_tmp_phone"] = phone
		}
	}
	return moved
}

func clearTmpTokens(state stateMap) {
	deleteKeys(state, tmpTokenKeys...)
	deleteKeys(state, tmpTokenMetaKeys...)
}

func (s *Server) tokenCheckReady(result map[string]any) bool {
	return anyBool(result["success"]) && anyBool(result["token_valid"]) && anyBool(result["has_min_balance"])
}

func (s *Server) tokenCheckValid(result map[string]any) bool {
	return anyBool(result["token_valid"])
}

func (s *Server) tokenCheckError(result map[string]any) string {
	if err := anyString(result["error"]); err != "" {
		return err
	}
	amount := anyInt(result["balance_amount"])
	currency := firstNonEmpty(anyString(result["balance_currency"]), "IDR")
	return fmt.Sprintf("insufficient gopay balance: %d %s < %d IDR", amount, currency, s.cfg.MinBalanceRp)
}
