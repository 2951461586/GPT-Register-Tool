package appsvc

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/byte-v-forge/gpt/gopay/protocol"
)

func (s *Server) deactivateStart(ctx context.Context, state stateMap, pin string) map[string]any {
	if !tmpTokenUsable(state, 30*time.Second) {
		return map[string]any{"success": false, "error": "temporary account token missing"}
	}
	client, err := s.tmpClientForState(ctx, state)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	profile, _ := client.Get(ctx, customerBaseURL+"/v1/users/profile")
	pinSetup := false
	if profile != nil && profile.StatusCode == http.StatusOK {
		pinSetup = boolForAnyKey(profile.Data(), "is_pin_setup", "isPinSetup")
	} else if strings.TrimSpace(pin) != "" {
		pinSetup = true
	}
	if pinSetup {
		pin = strings.TrimSpace(pin)
		if pin == "" {
			return map[string]any{"success": false, "error": "gopay pin missing"}
		}
		challenge, err := client.Post(ctx, customerBaseURL+"/api/v1/users/pin/challenges", map[string]any{"flow": "deactivation"})
		if err != nil {
			return map[string]any{"success": false, "error": err.Error()}
		}
		if challenge.StatusCode != http.StatusOK {
			return map[string]any{"success": false, "error": apiError("deactivation challenge failed", challenge)}
		}
		challengeID := challengeIDFrom(challenge.Data())
		clientID := clientIDFrom(challenge.Data())
		if challengeID == "" || clientID == "" {
			shape := responseShape(challenge)
			return map[string]any{"success": false, "error": "deactivation challenge missing id: " + safeJSON(shape), "response_shape": shape}
		}
		verify, err := client.Post(ctx, customerBaseURL+"/api/v1/users/pin/tokens", map[string]any{"challenge_id": challengeID, "client_id": clientID, "pin": pin})
		if err != nil {
			return map[string]any{"success": false, "error": err.Error()}
		}
		if verify.StatusCode != http.StatusOK {
			return map[string]any{"success": false, "error": apiError("deactivation pin verify failed", verify)}
		}
	}
	check, err := client.Get(ctx, customerBaseURL+"/api/v1/users/deactivate/check")
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if !deactivationCheckReady(check) {
		return map[string]any{"success": false, "error": apiError("deactivation check failed", check)}
	}
	state["stage"] = "deactivate_otp_pending"
	delete(state, "last_error")
	return map[string]any{"success": true, "otp_sent": true}
}

func (s *Server) deactivateComplete(ctx context.Context, state stateMap, otp string) map[string]any {
	if strings.TrimSpace(otp) == "" {
		return map[string]any{"success": false, "error": "otp required"}
	}
	client, err := s.tmpClientForState(ctx, state)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	resp, err := client.Delete(ctx, customerBaseURL+"/api/v1/users/deactivate", map[string]any{
		"otp":         strings.TrimSpace(otp),
		"reason":      "I no longer need digital payment services",
		"description": nil,
	})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if resp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("deactivate failed", resp)}
	}
	deactivatedAt := time.Now().Unix()
	state["deactivated_at"] = deactivatedAt
	state["stage"] = "deactivated"
	delete(state, "last_error")
	clearTmpTokens(state)
	return map[string]any{"success": true, "deactivated_at": deactivatedAt}
}

func deactivationCheckReady(resp *protocol.Response) bool {
	if resp == nil {
		return false
	}
	if resp.StatusCode == http.StatusOK || resp.StatusCode == 462 {
		return true
	}
	for _, errItem := range responseErrors(resp) {
		if strings.Contains(strings.ToUpper(fmt.Sprint(errItem)), "GOPAY-1603") {
			return true
		}
	}
	return false
}
