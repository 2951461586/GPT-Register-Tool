package gateway

import (
	"context"
	"time"
)

type Config struct {
	ChatGPTBase       string
	StripeBase        string
	Timeout           time.Duration
	PythonBin         string
	PythonExecutor    string
	Country           string
	Currency          string
	MaxAttempts       int
	CheckoutParallel  int
	RaceParallel      int
	ProxyRotations    int
	AllowNonZero      bool
	UsePythonFallback bool
}

func DefaultConfig() Config {
	return Config{
		ChatGPTBase:       "https://chatgpt.com",
		StripeBase:        "https://api.stripe.com",
		Timeout:           10 * time.Second,
		PythonBin:         ".venv/bin/python",
		PythonExecutor:    "python_executor.py",
		Country:           "US",
		Currency:          "USD",
		MaxAttempts:       1,
		CheckoutParallel:  1,
		RaceParallel:      1,
		ProxyRotations:    1,
		AllowNonZero:      true,
		UsePythonFallback: false,
	}
}

type Extractor struct {
	Config          Config
	extractOnceHook func(context.Context, string, string) (*ExtractResult, error)
	probeExitIPHook func(context.Context, string) (string, error)
	probeGeoHook    func(context.Context, string) (*ProxyGeo, error)
}

func NewExtractor(config Config) *Extractor {
	if config.ChatGPTBase == "" {
		config.ChatGPTBase = "https://chatgpt.com"
	}
	if config.StripeBase == "" {
		config.StripeBase = "https://api.stripe.com"
	}
	if config.Timeout <= 0 {
		config.Timeout = 10 * time.Second
	}
	if config.PythonBin == "" {
		config.PythonBin = ".venv/bin/python"
	}
	if config.PythonExecutor == "" {
		config.PythonExecutor = "python_executor.py"
	}
	if config.Country == "" {
		config.Country = "US"
	}
	if config.Currency == "" {
		config.Currency = "USD"
	}
	if config.MaxAttempts <= 0 {
		config.MaxAttempts = 1
	}
	if config.CheckoutParallel <= 0 {
		config.CheckoutParallel = 1
	}
	if config.RaceParallel <= 0 {
		config.RaceParallel = 1
	}
	if config.ProxyRotations <= 0 {
		config.ProxyRotations = 1
	}
	return &Extractor{Config: config}
}

type ExtractResult struct {
	OK                 bool   `json:"ok"`
	Code               string `json:"code"`
	Message            string `json:"message,omitempty"`
	ZeroVerified       bool   `json:"zero_verified"`
	AmountDue          *int64 `json:"amount_due,omitempty"`
	Currency           string `json:"currency,omitempty"`
	AmountDisplay      string `json:"amount_display"`
	HostedCheckoutURL  string `json:"hosted_checkout_url,omitempty"`
	PayPalAuthorizeURL string `json:"paypal_authorize_url,omitempty"`
	ProxyScheme        string `json:"proxy_scheme,omitempty"`
	ProxyIP            string `json:"proxy_ip,omitempty"`
	ProxyCountry       string `json:"proxy_country,omitempty"`
	ProxyRegion        string `json:"proxy_region,omitempty"`
	ProxyCity          string `json:"proxy_city,omitempty"`
	ProxyOrg           string `json:"proxy_org,omitempty"`
	ElapsedMS          int64  `json:"elapsed_ms,omitempty"`
	PublishableKey     string `json:"-"`
}

type APIError struct {
	Code    string
	Message string
	Status  int
}

type ProxyGeo struct {
	IP      string `json:"ip"`
	Country string `json:"country,omitempty"`
	Region  string `json:"region,omitempty"`
	City    string `json:"city,omitempty"`
	Org     string `json:"org,omitempty"`
}

func (e *APIError) Error() string {
	return e.Message
}
