package appsvc

import (
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	customerBaseURL = "https://customer.gopayapi.com"
	gojekBaseURL    = "https://api.gojekapi.com"
	gotoAuthBaseURL = "https://accounts.goto-products.com"
	appGoPayHost    = "app.gopay.co.id"
)

type Config struct {
	Port                       string
	StateDSN                   string
	StateTable                 string
	SignupAuthUUID             string
	PINClientID                string
	GotoClientID               string
	GotoClientSecret           string
	DynamicEgress              []string
	ProxyRuntimeHTTPAddr       string
	SignupInitiateJitterMin    time.Duration
	SignupInitiateJitterMax    time.Duration
	SignupRateLimitCooldown    time.Duration
	OTPTimeout                 time.Duration
	TokenRefreshMinTTL         time.Duration
	ChangePhoneConfirmTimeout  time.Duration
	ChangePhoneConfirmInterval time.Duration
	EnvelopeShortlinkTimeout   time.Duration
	ChangePhoneCountrySync     bool
	MinBalanceRp               int64
}

func ConfigFromEnv() Config {
	stateDSN := firstNonEmpty(
		os.Getenv("GOPAY_APP_PG_DSN"),
		os.Getenv("GOPAY_STATE_PG_DSN"),
		os.Getenv("PG_DSN"),
	)
	return Config{
		Port:                       firstNonEmpty(os.Getenv("GOPAY_APP_PORT"), "50051"),
		StateDSN:                   stateDSN,
		StateTable:                 firstNonEmpty(os.Getenv("GOPAY_STATE_TABLE"), "gopay_app_states"),
		SignupAuthUUID:             "bb648413-b637-443a-8ebf-176cf9b5dc32",
		PINClientID:                "6d11d261d7ae462dbd4be0dc5f36a697-MFAGOJEK",
		GotoClientID:               "gopay:consumer:app",
		GotoClientSecret:           "raOUumeMRBNifqvZRFjvsgTnjAlaA9",
		OTPTimeout:                 180 * time.Second,
		TokenRefreshMinTTL:         900 * time.Second,
		ChangePhoneConfirmTimeout:  8 * time.Second,
		ChangePhoneConfirmInterval: time.Second,
		EnvelopeShortlinkTimeout:   10 * time.Second,
		DynamicEgress:              splitDynamicEgress(os.Getenv("GOPAY_DYNAMIC_EGRESS")),
		ProxyRuntimeHTTPAddr:       strings.TrimSpace(os.Getenv("PROXY_RUNTIME_HTTP_ADDR")),
		SignupInitiateJitterMin:    envSeconds("GOPAY_SIGNUP_INITIATE_JITTER_MIN_SECONDS", 8),
		SignupInitiateJitterMax:    envSeconds("GOPAY_SIGNUP_INITIATE_JITTER_MAX_SECONDS", 25),
		SignupRateLimitCooldown:    envSeconds("GOPAY_SIGNUP_RATE_LIMIT_COOLDOWN_SECONDS", 900),
		MinBalanceRp:               1,
	}
}

func envSeconds(name string, fallback int64) time.Duration {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return time.Duration(fallback) * time.Second
	}
	parsed, err := strconv.ParseInt(value, 10, 64)
	if err != nil || parsed < 0 {
		return time.Duration(fallback) * time.Second
	}
	return time.Duration(parsed) * time.Second
}

func envFloatSeconds(name string, fallback float64) time.Duration {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return time.Duration(fallback * float64(time.Second))
	}
	parsed, err := strconv.ParseFloat(value, 64)
	if err != nil || parsed < 0 {
		return time.Duration(fallback * float64(time.Second))
	}
	return time.Duration(parsed * float64(time.Second))
}

func envBool(name string, fallback bool) bool {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	switch strings.ToLower(value) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

func splitDynamicEgress(raw string) []string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil
	}
	parts := regexp.MustCompile(`[\s,]+`).Split(raw, -1)
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		if value := strings.TrimSpace(part); value != "" {
			out = append(out, value)
		}
	}
	return out
}
