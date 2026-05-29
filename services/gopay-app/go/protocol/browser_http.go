package protocol

import (
	"bytes"
	"crypto/rand"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"os"
	"strings"
	"time"

	fhttp "github.com/bogdanfinn/fhttp"
	tlsclient "github.com/bogdanfinn/tls-client"
	"github.com/bogdanfinn/tls-client/profiles"
)

type browserHTTPClient struct {
	client         tlsclient.HttpClient
	cookieJar      fhttp.CookieJar
	proxyRawURL    string
	timeout        time.Duration
	tlsProfileName string
}

func NewBrowserHTTPClient(timeout time.Duration, proxyRawURL string, tlsProfileName ...string) (HTTPDoer, error) {
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	proxyRawURL = strings.TrimSpace(proxyRawURL)
	profileName := ResolveTLSProfileName(firstNonEmpty(tlsProfileName...))
	transport := &browserHTTPClient{
		cookieJar:      tlsclient.NewCookieJar(),
		proxyRawURL:    proxyRawURL,
		timeout:        timeout,
		tlsProfileName: profileName,
	}
	client, err := transport.newTLSClient()
	if err != nil {
		return nil, err
	}
	transport.client = client
	return transport, nil
}

func (c *browserHTTPClient) Do(req *http.Request) (*http.Response, error) {
	var body []byte
	if req.Body != nil {
		var err error
		body, err = io.ReadAll(req.Body)
		_ = req.Body.Close()
		if err != nil {
			return nil, err
		}
	}
	next, err := fhttp.NewRequestWithContext(req.Context(), req.Method, req.URL.String(), bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	next.Header = toFHTTPHeader(req.Header, req.Host)
	if req.Host != "" {
		next.Host = req.Host
	}
	resp, err := c.client.Do(next)
	if err != nil {
		return nil, err
	}
	status := resp.Status
	if strings.TrimSpace(status) == "" {
		status = fmt.Sprintf("%d %s", resp.StatusCode, http.StatusText(resp.StatusCode))
	}
	return &http.Response{
		Status:        status,
		StatusCode:    resp.StatusCode,
		Header:        fromFHTTPHeader(resp.Header),
		Body:          resp.Body,
		ContentLength: resp.ContentLength,
		Request:       req,
	}, nil
}

func (c *browserHTTPClient) newTLSClient() (tlsclient.HttpClient, error) {
	profileName := ResolveTLSProfileName(c.tlsProfileName)
	c.tlsProfileName = profileName
	profile, _ := lookupTLSProfile(profileName)
	options := []tlsclient.HttpClientOption{
		tlsclient.WithTimeoutSeconds(int(c.timeout.Seconds())),
		tlsclient.WithClientProfile(profile),
		tlsclient.WithCookieJar(c.cookieJar),
	}
	if envBoolDefault("GOPAY_TLS_RANDOM_EXTENSION_ORDER", false) {
		options = append(options, tlsclient.WithRandomTLSExtensionOrder())
	}
	if envBoolDefault("GOPAY_TLS_DISABLE_HTTP3", true) {
		options = append(options, tlsclient.WithDisableHttp3())
	}
	if envBoolDefault("GOPAY_TLS_FORCE_HTTP1", false) {
		options = append(options, tlsclient.WithForceHttp1())
	}
	if c.proxyRawURL != "" {
		options = append(options, tlsclient.WithProxyUrl(c.proxyRawURL))
	}
	return tlsclient.NewHttpClient(tlsclient.NewNoopLogger(), options...)
}

var defaultAndroidTLSProfileNames = []string{
	"okhttp4_android_10",
	"okhttp4_android_11",
	"okhttp4_android_12",
	"okhttp4_android_13",
	"zalando_android_mobile",
	"nike_android_mobile",
	"confirmed_android",
	"mesh_android",
}

func SelectTLSProfileName() string {
	if profileName := strings.TrimSpace(os.Getenv("GOPAY_TLS_PROFILE")); profileName != "" && !strings.EqualFold(profileName, "random") {
		if canonical, ok := canonicalTLSProfileName(profileName); ok {
			return canonical
		}
	}
	return randomTLSProfileName()
}

func ResolveTLSProfileName(profileName string) string {
	profileName = strings.TrimSpace(profileName)
	if profileName != "" && !strings.EqualFold(profileName, "random") {
		if canonical, ok := canonicalTLSProfileName(profileName); ok {
			return canonical
		}
	}
	return SelectTLSProfileName()
}

func randomTLSProfileName() string {
	if profilesFromEnv := tlsProfilesFromEnv(); len(profilesFromEnv) > 0 {
		return profilesFromEnv[randomProfileIndex(len(profilesFromEnv))]
	}
	return defaultAndroidTLSProfileNames[randomProfileIndex(len(defaultAndroidTLSProfileNames))]
}

func tlsProfilesFromEnv() []string {
	raw := strings.TrimSpace(os.Getenv("GOPAY_TLS_PROFILES"))
	if raw == "" {
		return nil
	}
	var out []string
	for _, part := range strings.Split(raw, ",") {
		name := strings.TrimSpace(part)
		if name == "" {
			continue
		}
		if canonical, ok := canonicalTLSProfileName(name); ok {
			out = append(out, canonical)
		}
	}
	return out
}

func lookupTLSProfile(name string) (profiles.ClientProfile, bool) {
	canonical, ok := canonicalTLSProfileName(name)
	if !ok {
		return profiles.ClientProfile{}, false
	}
	return profiles.MappedTLSClients[canonical], true
}

func canonicalTLSProfileName(name string) (string, bool) {
	for candidate := range profiles.MappedTLSClients {
		if strings.EqualFold(candidate, name) {
			return candidate, true
		}
	}
	return "", false
}

func randomProfileIndex(size int) int {
	if size <= 1 {
		return 0
	}
	n, err := rand.Int(rand.Reader, big.NewInt(int64(size)))
	if err != nil {
		return int(time.Now().UnixNano() % int64(size))
	}
	return int(n.Int64())
}

func envBoolDefault(name string, fallback bool) bool {
	value := strings.ToLower(strings.TrimSpace(os.Getenv(name)))
	switch value {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}

func toFHTTPHeader(src http.Header, host string) fhttp.Header {
	dst := make(fhttp.Header)
	for key, values := range src {
		for _, value := range values {
			dst.Add(key, value)
		}
	}
	if isGoPaySignedHeader(dst) {
		dst[fhttp.HeaderOrderKey] = gopaySignedHeaderOrder(dst, host)
		dst[fhttp.PHeaderOrderKey] = []string{":method", ":authority", ":scheme", ":path"}
	}
	return dst
}

func isGoPaySignedHeader(headers fhttp.Header) bool {
	return headerValue(headers, "x-e1") != "" && strings.EqualFold(headerValue(headers, "x-appid"), "com.gojek.gopay")
}

func headerValue(headers fhttp.Header, key string) string {
	for existing, values := range headers {
		if !strings.EqualFold(existing, key) {
			continue
		}
		for _, value := range values {
			if value = strings.TrimSpace(value); value != "" {
				return value
			}
		}
		return ""
	}
	return ""
}

func gopaySignedHeaderOrder(headers fhttp.Header, host string) []string {
	if strings.EqualFold(firstNonEmpty(host, headerValue(headers, "host")), "accounts.goto-products.com") {
		return []string{
			"accept-encoding",
			"key",
			"x-cvsdk-version",
			"authorization",
			"verification-token",
			"is-token-required",
			"gojek-service-area",
			"x-request-id",
			"country-code",
			"x-appversion",
			"content-length",
			"x-m1",
			"gojek-country-code",
			"x-uniqueid",
			"x-phonemake",
			"x-help-version",
			"x-e1",
			"user-agent",
			"x-deviceos",
			"x-user-type",
			"x-appid",
			"gojek-timezone",
			"content-type",
			"x-authsdk-version",
			"x-apptype",
			"x-user-locale",
			"x-devicetoken",
			"x-e2",
			"accept-language",
			"host",
			"transaction-id",
			"x-phonemodel",
			"x-platform",
		}
	}
	return []string{
		"accept-encoding",
		"country-code",
		"gojek-country-code",
		"gojek-service-area",
		"x-appversion",
		"x-help-version",
		"x-location",
		"x-location-accuracy",
		"x-uniqueid",
		"x-phonemake",
		"x-phonemodel",
		"x-deviceos",
		"x-user-type",
		"x-appid",
		"gojek-timezone",
		"x-apptype",
		"x-user-locale",
		"accept-language",
		"x-platform",
		"user-agent",
		"content-type",
		"x-m1",
		"x-e2",
		"x-authsdk-version",
		"x-cvsdk-version",
		"authorization",
		"x-request-id",
		"transaction-id",
		"verification-token",
		"is-token-required",
		"x-e1",
	}
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value = strings.TrimSpace(value); value != "" {
			return value
		}
	}
	return ""
}

func fromFHTTPHeader(src fhttp.Header) http.Header {
	dst := make(http.Header)
	for key, values := range src {
		for _, value := range values {
			dst.Add(key, value)
		}
	}
	return dst
}
