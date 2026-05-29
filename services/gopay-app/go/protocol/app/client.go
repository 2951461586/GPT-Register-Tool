package app

import (
	"bytes"
	"context"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/byte-v-forge/gpt/gopay/protocol"
)

const CustomerBaseURL = "https://customer.gopayapi.com"

type Config struct {
	Token                 string
	ProxyURL              string
	Timeout               time.Duration
	HTTPClient            *http.Client
	Device                DeviceFingerprint
	DeviceConfig          DeviceConfig
	SignVersion           string
	LegacyHMACKey         string
	DisplayEncoderKey     string
	DisplayEncoderID      string
	SignedMsgTemplatePath string
	Logger                protocol.Logger
	DebugHTTP             bool
}

func ConfigFromEnv(token string) Config {
	return Config{
		Token:        token,
		ProxyURL:     os.Getenv("GOPAY_PROXY_URL"),
		DeviceConfig: DeviceConfigFromEnv(),
		DebugHTTP:    envBool("GOPAY_APP_DEBUG_HTTP_REQUESTS"),
		SignVersion:  firstNonEmpty(os.Getenv("GOPAY_SIGN_VERSION"), defaultGoPaySignVersion),
		LegacyHMACKey: firstNonEmpty(
			os.Getenv("GOPAY_LEGACY_DISPLAY_ENCODER_KEY"),
			os.Getenv("GOPAY_HMAC_KEY"),
			defaultGoPayLegacyDisplayEncoderKey,
		),
		DisplayEncoderKey:     firstNonEmpty(os.Getenv("GOPAY_DISPLAY_ENCODER_KEY"), defaultGoPayDisplayEncoderKey),
		DisplayEncoderID:      firstNonEmpty(os.Getenv("GOPAY_DISPLAY_ENCODER_ID"), defaultGoPayDisplayEncoderID),
		SignedMsgTemplatePath: strings.TrimSpace(os.Getenv("GOPAY_SIGNED_MSG_TEMPLATE")),
	}
}

type Client struct {
	token     string
	device    DeviceFingerprint
	http      *protocol.Client
	signer    Signer
	logger    protocol.Logger
	debugHTTP bool
}

func NewClient(cfg Config) (*Client, error) {
	device := cfg.Device
	if device.AppID == "" {
		var err error
		device, err = NewDeviceFingerprint(cfg.DeviceConfig)
		if err != nil {
			return nil, err
		}
	} else if device.TLSProfileName == "" {
		device.TLSProfileName = protocol.ResolveTLSProfileName("")
	}
	var httpClient protocol.HTTPDoer
	if cfg.HTTPClient != nil {
		httpClient = cfg.HTTPClient
	} else {
		var err error
		httpClient, err = protocol.NewBrowserHTTPClient(cfg.Timeout, cfg.ProxyURL, device.TLSProfileName)
		if err != nil {
			return nil, err
		}
	}
	base, err := protocol.NewClient("", protocol.WithHTTPDoer(httpClient), protocol.WithRetry(protocol.RetryPolicy{Attempts: 1}), protocol.WithLogger(cfg.Logger))
	if err != nil {
		return nil, err
	}
	return &Client{
		token:  strings.TrimSpace(cfg.Token),
		device: device,
		http:   base,
		signer: Signer{
			SignVersion:           cfg.SignVersion,
			LegacyHMACKey:         cfg.LegacyHMACKey,
			DisplayEncoderKey:     cfg.DisplayEncoderKey,
			DisplayEncoderID:      cfg.DisplayEncoderID,
			SignedMsgTemplatePath: cfg.SignedMsgTemplatePath,
		},
		logger:    cfg.Logger,
		debugHTTP: cfg.DebugHTTP,
	}, nil
}

func (c *Client) Device() DeviceFingerprint {
	return c.device
}

func (c *Client) Get(ctx context.Context, rawURL string, expected ...int) (*protocol.Response, error) {
	return c.request(ctx, http.MethodGet, rawURL, nil, nil, expected...)
}

func (c *Client) Post(ctx context.Context, rawURL string, body any, expected ...int) (*protocol.Response, error) {
	return c.request(ctx, http.MethodPost, rawURL, body, nil, expected...)
}

func (c *Client) Patch(ctx context.Context, rawURL string, body any, expected ...int) (*protocol.Response, error) {
	return c.request(ctx, http.MethodPatch, rawURL, body, nil, expected...)
}

func (c *Client) Put(ctx context.Context, rawURL string, body any, expected ...int) (*protocol.Response, error) {
	return c.request(ctx, http.MethodPut, rawURL, body, nil, expected...)
}

func (c *Client) Delete(ctx context.Context, rawURL string, body any, expected ...int) (*protocol.Response, error) {
	return c.request(ctx, http.MethodDelete, rawURL, body, nil, expected...)
}

func (c *Client) Request(ctx context.Context, method string, rawURL string, body any, extra http.Header, expected ...int) (*protocol.Response, error) {
	return c.request(ctx, method, rawURL, body, extra, expected...)
}

func (c *Client) TokenizePINWeb(ctx context.Context, pin string, challengeID string, clientID string, expected ...int) (*protocol.Response, error) {
	rawURL := CustomerBaseURL + "/api/v1/users/pin/tokens/nb"
	bodyRaw, err := protocol.CompactJSON(map[string]any{
		"challenge_id": challengeID,
		"client_id":    clientID,
		"pin":          pin,
	})
	if err != nil {
		return nil, err
	}
	headers := pinWebHeaders()
	c.logHTTPRequest(ctx, http.MethodPost, rawURL, headers, bodyRaw)
	resp, err := c.http.Do(ctx, protocol.Request{
		Method:       http.MethodPost,
		Path:         rawURL,
		Body:         bodyRaw,
		Headers:      headers,
		Operation:    "gopay-pin-web",
		ExpectStatus: expected,
	})
	c.logHTTPResponse(ctx, http.MethodPost, rawURL, resp, err)
	return resp, err
}

func (c *Client) request(ctx context.Context, method string, rawURL string, body any, extra http.Header, expected ...int) (*protocol.Response, error) {
	bodyRaw, err := protocol.CompactJSON(body)
	if err != nil {
		return nil, err
	}
	headers, err := c.headers(method, rawURL, bodyRaw, extra)
	if err != nil {
		return nil, err
	}
	c.logHTTPRequest(ctx, method, rawURL, headers, bodyRaw)
	resp, err := c.http.Do(ctx, protocol.Request{
		Method:       method,
		Path:         rawURL,
		Body:         bodyRaw,
		Headers:      headers,
		Operation:    "gopay-app",
		ExpectStatus: expected,
	})
	c.logHTTPResponse(ctx, method, rawURL, resp, err)
	return resp, err
}

func (c *Client) headers(method string, rawURL string, body []byte, extra http.Header) (http.Header, error) {
	parsed, _ := url.Parse(rawURL)
	path := parsed.Path
	host := strings.ToLower(parsed.Host)
	requestHost := parsed.Host
	xM1 := c.device.XM1()
	hasBody := len(body) > 0
	headers := http.Header{}
	setBaseHeaders(headers, c.device, xM1, hasBody)
	signatureVersion := c.signer.signVersionForRequest(rawURL)
	includeBodyMD5Header := signatureVersion != "v2" && !omitBodyMD5Header(host, path)

	if host == "gwa.gopayapi.com" {
		headers = gopayGatewayHeaders(hasBody)
		for key, values := range extra {
			setHeaderValues(headers, key, values)
		}
		if requestHost != "" {
			setHeader(headers, "Host", requestHost)
		}
		return headers, nil
	}

	if host == "accounts.goto-products.com" {
		headers = gotoAuthHeaders(c.device, xM1, hasBody)
		if strings.HasPrefix(path, "/cvs/") {
			setHeader(headers, "Authorization", "")
		}
		if path == "/cvs/v1/initiate" && bytes.Contains(body, []byte(`"flow":"signup"`)) {
			setHeader(headers, "Key", "value")
		}
	} else if host == "customer.gopayapi.com" && path == "/v1/support/customer/initiate" {
		headers = supportCustomerHeaders(c.device, xM1, hasBody)
	} else if host == "api.gojekapi.com" && (gojekActivityPaths[path] || gojekAppHeaderPaths[path]) {
		headers = appHeaders(c.device, xM1, hasBody)
	} else if host == "customer.gopayapi.com" && (isGopayCustomerLinkPath(path) || isGopayCustomerAppHeaderPath(path) || (method == http.MethodGet && gopayCustomerSlimGetPaths[path])) {
		headers = appHeaders(c.device, xM1, hasBody)
	} else {
		setHeader(headers, "User-uuid", c.device.UserUUID)
		setHeader(headers, "X-DeviceToken", c.device.DeviceToken)
		setHeader(headers, "X-IMEI", c.device.IMEI)
		setHeader(headers, "X-IpAddress", c.device.IPAddress)
		setHeader(headers, "X-Location", c.device.Location)
		setHeader(headers, "X-Location-Accuracy", c.device.LocationAccuracy)
		setHeader(headers, "Gojek-Country-Code", c.device.GojekCountryCode)
		setHeader(headers, "X-Dark-Mode", "false")
	}
	if path == "/api/v1/users/pin/tokens" {
		setHeader(headers, "Sdk-Version", c.device.AppVersion)
		setHeader(headers, "X-Biometric", "")
		setHeader(headers, "X-Verification", "PIN")
	}
	if c.token != "" {
		if strings.HasPrefix(c.token, "Bearer ") {
			setHeader(headers, "Authorization", c.token)
		} else {
			setHeader(headers, "Authorization", "Bearer "+c.token)
		}
	}
	for key, values := range extra {
		setHeaderValues(headers, key, values)
	}
	if requestHost != "" {
		setHeader(headers, "Host", requestHost)
	}
	signToken := headers.Get("Authorization")
	if signToken == "" {
		signToken = c.token
	}
	signature, err := c.signer.Sign(method, rawURL, body, signToken, c.device, xM1)
	if err != nil {
		return nil, err
	}
	setHeader(headers, "X-E1", signature.XE1)
	if includeBodyMD5Header {
		setHeader(headers, "X-E3", signature.BodyMD5)
	}
	return headers, nil
}

func gopayGatewayHeaders(hasBody bool) http.Header {
	headers := http.Header{}
	setHeader(headers, "Accept", "application/json, text/plain, */*")
	setHeader(headers, "Origin", "https://merchants-gws-app.gopayapi.com")
	setHeader(headers, "Referer", "https://merchants-gws-app.gopayapi.com/")
	setHeader(headers, "X-User-Locale", defaultUserLocale)
	if hasBody {
		setHeader(headers, "Content-Type", "application/json")
	}
	return headers
}

func pinWebHeaders() http.Header {
	headers := http.Header{}
	setHeader(headers, "Accept", "application/json, text/plain, */*")
	setHeader(headers, "Content-Type", "application/json")
	setHeader(headers, "Origin", "https://pin-web-client.gopayapi.com")
	setHeader(headers, "Referer", "https://pin-web-client.gopayapi.com/")
	setHeader(headers, "X-AppVersion", "1.0.0")
	setHeader(headers, "X-Correlation-ID", uuid.NewString())
	setHeader(headers, "X-Is-Mobile", "false")
	setHeader(headers, "X-Platform", "Mac OS 12.2.1")
	setHeader(headers, "X-Request-ID", uuid.NewString())
	setHeader(headers, "X-User-Locale", "id")
	return headers
}

func setHeader(headers http.Header, key, value string) {
	deleteHeader(headers, key)
	headers[key] = []string{value}
}

func setHeaderValues(headers http.Header, key string, values []string) {
	deleteHeader(headers, key)
	headers[key] = append([]string(nil), values...)
}

func deleteHeader(headers http.Header, key string) {
	for existing := range headers {
		if strings.EqualFold(existing, key) {
			delete(headers, existing)
		}
	}
}

func setBaseHeaders(headers http.Header, device DeviceFingerprint, xM1 string, hasBody bool) {
	setHeader(headers, "X-AppVersion", device.AppVersion)
	setHeader(headers, "X-AppId", device.AppID)
	setHeader(headers, "X-AppType", device.AppType)
	setHeader(headers, "Accept", "application/json")
	setHeader(headers, "User-Agent", device.UserAgent)
	setHeader(headers, "D1", device.D1)
	setHeader(headers, "X-Session-ID", device.SessionID)
	setHeader(headers, "X-Platform", device.Platform)
	setHeader(headers, "X-UniqueId", device.UniqueID)
	setHeader(headers, "X-User-Type", device.UserType)
	setHeader(headers, "X-DeviceOS", device.DeviceOS)
	setHeader(headers, "X-PhoneMake", device.PhoneMake)
	setHeader(headers, "X-PushTokenType", "FCM")
	setHeader(headers, "X-DeviceToken", device.DeviceToken)
	setHeader(headers, "X-IMEI", device.IMEI)
	setHeader(headers, "X-IpAddress", device.IPAddress)
	setHeader(headers, "X-PhoneModel", device.PhoneModel)
	setHeader(headers, "Accept-Language", defaultAcceptLanguage)
	setHeader(headers, "X-User-Locale", defaultUserLocale)
	setHeader(headers, "X-M1", xM1)
	setHeader(headers, "X-E2", device.XE2)
	setHeader(headers, "AdjTs", device.AdjTS)
	if hasBody {
		setHeader(headers, "Content-Type", "application/json")
	}
}

func appHeaders(device DeviceFingerprint, xM1 string, hasBody bool) http.Header {
	headers := http.Header{}
	setHeader(headers, "Accept-Encoding", "gzip")
	setHeader(headers, "Gojek-Service-Area", "1")
	setHeader(headers, "Country-Code", device.GojekCountryCode)
	setHeader(headers, "X-AppVersion", device.AppVersion)
	setHeader(headers, "X-M1", xM1)
	setHeader(headers, "Gojek-Country-Code", device.GojekCountryCode)
	setHeader(headers, "X-Request-ID", newTimeUUIDString())
	setHeader(headers, "X-UniqueId", device.UniqueID)
	setHeader(headers, "X-IMEI", device.IMEI)
	setHeader(headers, "X-IpAddress", device.IPAddress)
	setHeader(headers, "X-PhoneMake", device.PhoneMake)
	setHeader(headers, "X-Help-Version", device.AppVersion)
	setHeader(headers, "X-DeviceToken", device.DeviceToken)
	setHeader(headers, "X-Location", device.Location)
	setHeader(headers, "X-Location-Accuracy", device.LocationAccuracy)
	setHeader(headers, "X-DeviceOS", device.DeviceOS)
	setHeader(headers, "X-User-Type", device.UserType)
	setHeader(headers, "User-Agent", device.UserAgent)
	setHeader(headers, "X-AppId", device.AppID)
	setHeader(headers, "Gojek-Timezone", defaultTimezone)
	setHeader(headers, "X-AuthSDK-Version", defaultAuthSDKVersion)
	setHeader(headers, "X-AppType", device.AppType)
	setHeader(headers, "X-User-Locale", defaultUserLocale)
	setHeader(headers, "X-E2", device.XE2)
	setHeader(headers, "X-CVSDK-Version", defaultCVSDKVersion)
	setHeader(headers, "Accept-Language", defaultAcceptLanguage)
	setHeader(headers, "Transaction-ID", device.TransactionID)
	setHeader(headers, "X-PhoneModel", device.PhoneModel)
	setHeader(headers, "X-Platform", device.Platform)
	if hasBody {
		setHeader(headers, "Content-Type", "application/json")
	}
	return headers
}

func gotoAuthHeaders(device DeviceFingerprint, xM1 string, hasBody bool) http.Header {
	headers := http.Header{}
	setHeader(headers, "Accept-Encoding", "gzip")
	setHeader(headers, "X-CVSDK-Version", defaultCVSDKVersion)
	setHeader(headers, "Gojek-Service-Area", "1")
	setHeader(headers, "X-Request-ID", newTimeUUIDString())
	setHeader(headers, "Country-Code", device.GojekCountryCode)
	setHeader(headers, "X-AppVersion", device.AppVersion)
	setHeader(headers, "X-M1", xM1)
	setHeader(headers, "Gojek-Country-Code", device.GojekCountryCode)
	setHeader(headers, "X-UniqueId", device.UniqueID)
	setHeader(headers, "X-PhoneMake", device.PhoneMake)
	setHeader(headers, "X-Help-Version", device.AppVersion)
	setHeader(headers, "User-Agent", device.UserAgent)
	setHeader(headers, "X-DeviceOS", device.DeviceOS)
	setHeader(headers, "X-User-Type", device.UserType)
	setHeader(headers, "X-AppId", device.AppID)
	setHeader(headers, "Gojek-Timezone", defaultTimezone)
	setHeader(headers, "X-AuthSDK-Version", defaultAuthSDKVersion)
	setHeader(headers, "X-AppType", device.AppType)
	setHeader(headers, "X-User-Locale", defaultUserLocale)
	setHeader(headers, "X-DeviceToken", device.DeviceToken)
	setHeader(headers, "X-E2", device.XE2)
	setHeader(headers, "Accept-Language", defaultAcceptLanguage)
	setHeader(headers, "Transaction-ID", device.TransactionID)
	setHeader(headers, "X-PhoneModel", device.PhoneModel)
	setHeader(headers, "X-Platform", device.Platform)
	if hasBody {
		setHeader(headers, "Content-Type", "application/json")
	}
	return headers
}

func supportCustomerHeaders(device DeviceFingerprint, xM1 string, hasBody bool) http.Header {
	headers := http.Header{}
	setHeader(headers, "Accept-Encoding", "gzip")
	setHeader(headers, "Gojek-Service-Area", "1")
	setHeader(headers, "Country-Code", device.GojekCountryCode)
	setHeader(headers, "Support-Request-Id", newTimeUUIDString())
	setHeader(headers, "X-AppVersion", device.AppVersion)
	setHeader(headers, "X-M1", xM1)
	setHeader(headers, "Gojek-Country-Code", device.GojekCountryCode)
	setHeader(headers, "X-UniqueId", device.UniqueID)
	setHeader(headers, "X-PhoneMake", device.PhoneMake)
	setHeader(headers, "X-Help-Version", device.AppVersion)
	setHeader(headers, "User-Agent", device.UserAgent)
	setHeader(headers, "X-DeviceOS", device.DeviceOS)
	setHeader(headers, "X-User-Type", device.UserType)
	setHeader(headers, "X-AppId", device.AppID)
	setHeader(headers, "Gojek-Timezone", defaultTimezone)
	setHeader(headers, "X-AppType", device.AppType)
	setHeader(headers, "X-User-Locale", defaultUserLocale)
	setHeader(headers, "X-DeviceToken", device.DeviceToken)
	setHeader(headers, "X-E2", device.XE2)
	setHeader(headers, "Accept-Language", defaultAcceptLanguage)
	setHeader(headers, "X-PhoneModel", device.PhoneModel)
	setHeader(headers, "Support-SDK-Version", defaultSupportSDK)
	setHeader(headers, "X-Platform", device.Platform)
	if hasBody {
		setHeader(headers, "Content-Type", "application/json")
	}
	return headers
}

func omitBodyMD5Header(host, path string) bool {
	switch strings.ToLower(host) {
	case "accounts.goto-products.com":
		return true
	case "customer.gopayapi.com":
		return path == "/v1/support/customer/initiate"
	default:
		return false
	}
}

func newTimeUUIDString() string {
	value, err := uuid.NewUUID()
	if err == nil {
		return value.String()
	}
	return uuid.NewString()
}

var gopayCustomerSlimGetPaths = map[string]bool{
	"/v1/users/profile":            true,
	"/v1/payment-options/balances": true,
	"/v1/payment-options/profiles": true,
	"/v1/user/wallet-card/balance": true,
}

var gopayCustomerAppHeaderPaths = map[string]bool{
	"/v1/users/profile":                               true,
	"/v1/qris/payments":                               true,
	"/v2/customer/payment-options/checkout/list":      true,
	"/v1/customer/payment-options/settings/last-used": true,
	"/v1/promotions/evaluate":                         true,
	"/api/v1/festival-envelopes/claim":                true,
	"/api/v1/users/deactivate":                        true,
	"/api/v1/users/deactivate/check":                  true,
	"/api/v1/users/pin/challenges":                    true,
	"/api/v1/users/pin/tokens":                        true,
	"/api/v1/users/pin/tokens/nb":                     true,
	"/api/v1/users/pins/allowed":                      true,
	"/api/v2/users/pins/setup/tokens":                 true,
	"/cvs/v1/methods":                                 true,
	"/cvs/v1/initiate":                                true,
	"/cvs/v1/verify":                                  true,
}

var gojekActivityPaths = map[string]bool{
	"/v5/customers": true,
	"/v2/otp/retry": true,
	"/v5/customers/verificationUpdateProfile": true,
	"/gojek/v2/customer":                      true,
}

var gojekAppHeaderPaths = map[string]bool{
	"/courier/v1/token":    true,
	"/v7/customers/signup": true,
}

func isGopayCustomerLinkPath(path string) bool {
	return path == "/v1/linkedapps" || strings.HasPrefix(path, "/v1/links/")
}

func isGopayCustomerAppHeaderPath(path string) bool {
	if gopayCustomerAppHeaderPaths[path] {
		return true
	}
	if path == "/v1/festivals" || strings.HasPrefix(path, "/v1/festivals/") {
		return true
	}
	if strings.HasPrefix(path, "/customers/v1/payments/") {
		return true
	}
	if strings.HasPrefix(path, "/v3/payments/") && strings.HasSuffix(path, "/capture") {
		return true
	}
	if strings.HasPrefix(path, "/api/v2/challenges/") && (strings.HasSuffix(path, "/pin-page") || strings.HasSuffix(path, "/pin-page/nb")) {
		return true
	}
	return false
}
