package appsvc

import (
	"context"
	"crypto/rand"
	"fmt"
	"math/big"
	"net/http"
	"time"

	"github.com/byte-v-forge/gpt/gopay/protocol"
)

const (
	signupRateLimitScopeProbe    = "signup_login_methods"
	signupRateLimitScopeMethods  = "signup_cvs_methods"
	signupRateLimitScopeInitiate = "signup_cvs_initiate"
)

func (s *Server) signupInitiateDelay() time.Duration {
	minDelay := s.cfg.SignupInitiateJitterMin
	maxDelay := s.cfg.SignupInitiateJitterMax
	if maxDelay <= 0 {
		return 0
	}
	if minDelay < 0 {
		minDelay = 0
	}
	if maxDelay < minDelay {
		minDelay, maxDelay = maxDelay, minDelay
	}
	if maxDelay == minDelay {
		return maxDelay
	}
	span := int64((maxDelay - minDelay) / time.Second)
	if span <= 0 {
		return minDelay
	}
	return minDelay + time.Duration(randomInt64(span+1))*time.Second
}

func sleepWithContext(ctx context.Context, delay time.Duration) error {
	if delay <= 0 {
		return nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func (s *Server) signupCooldownResult(state stateMap) map[string]any {
	until := stateInt(state, "_signup_cooldown_until")
	now := time.Now().Unix()
	if until <= now {
		if until > 0 {
			deleteKeys(
				state,
				"_signup_cooldown_until",
				"_signup_rate_limited_at",
				"_signup_rate_limit_scope",
				"_signup_rate_limit_status",
				"_signup_rate_limit_phone",
				"_signup_rate_limit_country_code",
			)
		}
		return nil
	}
	retryAfter := until - now
	scope := firstNonEmpty(stateString(state, "_signup_rate_limit_scope"), "signup")
	state["last_error"] = "SIGNUP_RATE_LIMITED"
	return map[string]any{
		"success":              false,
		"error":                fmt.Sprintf("signup cooldown active: scope=%s retry_after_seconds=%d", scope, retryAfter),
		"rate_limit_scope":     scope,
		"cooldown_until":       until,
		"retry_after_seconds":  retryAfter,
		"cooldown_state_reuse": true,
	}
}

func (s *Server) signupRateLimitResult(state stateMap, scope string, phone string, countryCode string, label string, resp *protocol.Response) map[string]any {
	now := time.Now()
	cooldown := s.signupRateLimitCooldown(resp)
	until := now.Add(cooldown).Unix()
	state["_signup_rate_limited_at"] = now.Unix()
	state["_signup_rate_limit_scope"] = scope
	state["_signup_rate_limit_status"] = statusCode(resp)
	state["_signup_rate_limit_phone"] = phone
	state["_signup_rate_limit_country_code"] = countryCode
	state["_signup_cooldown_until"] = until
	state["stage"] = "idle"
	state["last_error"] = "SIGNUP_RATE_LIMITED"
	return map[string]any{
		"success":             false,
		"error":               apiError(label, resp),
		"raw_json":            safeJSON(resp.Payload),
		"rate_limit_scope":    scope,
		"cooldown_until":      until,
		"cooldown_seconds":    int64(cooldown.Seconds()),
		"retry_after_seconds": int64(cooldown.Seconds()),
	}
}

func (s *Server) signupRateLimitCooldown(resp *protocol.Response) time.Duration {
	if retryAfter := retryTimerSeconds(resp); retryAfter > 0 {
		return time.Duration(retryAfter) * time.Second
	}
	if s.cfg.SignupRateLimitCooldown > 0 {
		return s.cfg.SignupRateLimitCooldown
	}
	return 15 * time.Minute
}

func retryTimerSeconds(resp *protocol.Response) int64 {
	if resp == nil {
		return 0
	}
	return maxRetryTimerSeconds(resp.Data()["retry_timer_in_seconds"])
}

func maxRetryTimerSeconds(value any) int64 {
	switch typed := value.(type) {
	case []any:
		var maxValue int64
		for _, item := range typed {
			if parsed := maxRetryTimerSeconds(item); parsed > maxValue {
				maxValue = parsed
			}
		}
		return maxValue
	default:
		return anyInt(value)
	}
}

func statusCode(resp *protocol.Response) int {
	if resp == nil {
		return 0
	}
	return resp.StatusCode
}

func randomInt64(maxExclusive int64) int64 {
	if maxExclusive <= 0 {
		return 0
	}
	n, err := rand.Int(rand.Reader, big.NewInt(maxExclusive))
	if err != nil {
		return time.Now().UnixNano() % maxExclusive
	}
	return n.Int64()
}

func rateLimitLabel(scope string) string {
	switch scope {
	case signupRateLimitScopeProbe:
		return "signup phone probe rate limited"
	case signupRateLimitScopeMethods:
		return "signup methods rate limited"
	case signupRateLimitScopeInitiate:
		return "signup otp initiate rate limited"
	default:
		return http.StatusText(http.StatusTooManyRequests)
	}
}
