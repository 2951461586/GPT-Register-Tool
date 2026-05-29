package appsvc

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type stateMap map[string]any

func parseState(raw string) (stateMap, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return stateMap{}, nil
	}
	var value map[string]any
	if err := json.Unmarshal([]byte(raw), &value); err != nil {
		return nil, err
	}
	if value == nil {
		value = map[string]any{}
	}
	return stateMap(value), nil
}

func stateJSON(state stateMap) string {
	if state == nil {
		state = stateMap{}
	}
	raw, err := json.Marshal(state)
	if err != nil {
		return "{}"
	}
	return string(raw)
}

func stateString(state stateMap, key string) string {
	if state == nil {
		return ""
	}
	switch value := state[key].(type) {
	case string:
		return strings.TrimSpace(value)
	case fmt.Stringer:
		return strings.TrimSpace(value.String())
	case nil:
		return ""
	default:
		return strings.TrimSpace(fmt.Sprint(value))
	}
}

func stateInt(state stateMap, key string) int64 {
	if state == nil {
		return 0
	}
	return anyInt(state[key])
}

func anyString(value any) string {
	switch typed := value.(type) {
	case string:
		return strings.TrimSpace(typed)
	case fmt.Stringer:
		return strings.TrimSpace(typed.String())
	case nil:
		return ""
	default:
		return strings.TrimSpace(fmt.Sprint(typed))
	}
}

func anyInt(value any) int64 {
	switch typed := value.(type) {
	case int:
		return int64(typed)
	case int64:
		return typed
	case float64:
		if math.IsNaN(typed) || math.IsInf(typed, 0) {
			return 0
		}
		return int64(typed)
	case string:
		text := strings.TrimSpace(typed)
		if text == "" {
			return 0
		}
		parsed, err := strconv.ParseFloat(text, 64)
		if err != nil {
			return 0
		}
		return int64(parsed)
	default:
		return 0
	}
}

func anyBool(value any) bool {
	switch typed := value.(type) {
	case bool:
		return typed
	case int:
		return typed != 0
	case int32:
		return typed != 0
	case int64:
		return typed != 0
	case float32:
		return typed != 0
	case float64:
		return typed != 0
	case string:
		switch strings.ToLower(strings.TrimSpace(typed)) {
		case "1", "true", "yes", "on":
			return true
		default:
			return false
		}
	default:
		return false
	}
}

func nestedMap(value any) map[string]any {
	if typed, ok := value.(map[string]any); ok && typed != nil {
		return typed
	}
	return map[string]any{}
}

func deleteKeys(state stateMap, keys ...string) {
	for _, key := range keys {
		delete(state, key)
	}
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}

func phoneCountryCode(cfg Config, explicit string) string {
	value := strings.TrimSpace(explicit)
	value = strings.TrimSpace(value)
	if value == "" {
		value = "62"
	}
	if strings.HasPrefix(value, "+") {
		return value
	}
	return "+" + value
}

func normalizePhone(phone string, countryCode string) string {
	prefix := strings.TrimPrefix(phoneCountryCode(Config{}, countryCode), "+")
	value := strings.TrimPrefix(strings.TrimSpace(phone), "+")
	if strings.HasPrefix(value, prefix) {
		return strings.TrimPrefix(value, prefix)
	}
	return value
}

func normalizePhoneWithConfig(cfg Config, phone string, countryCode string) string {
	prefix := strings.TrimPrefix(phoneCountryCode(cfg, countryCode), "+")
	value := strings.TrimPrefix(strings.TrimSpace(phone), "+")
	if strings.HasPrefix(value, prefix) {
		return strings.TrimPrefix(value, prefix)
	}
	return value
}

func jwtExpiresAt(token string) int64 {
	token = strings.TrimSpace(strings.TrimPrefix(token, "Bearer "))
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return 0
	}
	raw := parts[1]
	raw += strings.Repeat("=", (4-len(raw)%4)%4)
	decoded, err := base64.URLEncoding.DecodeString(raw)
	if err != nil {
		decoded, err = base64.RawURLEncoding.DecodeString(parts[1])
	}
	if err != nil {
		return 0
	}
	var payload map[string]any
	if err := json.Unmarshal(decoded, &payload); err != nil {
		return 0
	}
	return anyInt(payload["exp"])
}

func tokenUsable(state stateMap, key string, minTTL time.Duration) bool {
	token := stateString(state, key)
	if token == "" {
		return false
	}
	expiresAt := jwtExpiresAt(token)
	if expiresAt == 0 {
		return true
	}
	return expiresAt > time.Now().Add(minTTL).Unix()
}

func safeJSON(value any) string {
	raw, err := json.Marshal(value)
	if err != nil {
		return fmt.Sprintf(`{"marshal_error":%q}`, err.Error())
	}
	return string(raw)
}

func compactErrorDetail(value any) string {
	raw := safeJSON(value)
	if len(raw) > 800 {
		return raw[:800]
	}
	return raw
}

var digitsRE = regexp.MustCompile(`\D+`)

func signupSeed(phone string) string {
	digits := digitsRE.ReplaceAllString(phone, "")
	if len(digits) > 6 {
		digits = digits[len(digits)-6:]
	}
	return fmt.Sprintf("%s%d", digits, time.Now().Unix())
}

func signupNameFromSeed(seed string) string {
	const alphabet = "abcdefghijklmnopqrstuvwxyz"
	hexChars := regexp.MustCompile(`[^0-9a-f]`).ReplaceAllString(strings.ToLower(seed), "")
	if len(hexChars) < 2 {
		hexChars = fmt.Sprintf("%02s", hexChars)
	}
	hexChars = hexChars[len(hexChars)-2:]
	var out strings.Builder
	for _, ch := range hexChars {
		idx, _ := strconv.ParseInt(string(ch), 16, 64)
		out.WriteByte(alphabet[idx%int64(len(alphabet))])
	}
	return out.String()
}
