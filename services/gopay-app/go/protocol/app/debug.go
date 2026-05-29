package app

import (
	"context"
	"encoding/json"
	"net/http"
	"net/url"
	"strings"

	"github.com/byte-v-forge/gpt/gopay/protocol"
)

func envBool(name string) bool {
	switch strings.ToLower(strings.TrimSpace(getenv(name))) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}

func (c *Client) logHTTPRequest(ctx context.Context, method, rawURL string, headers http.Header, body []byte) {
	if !c.shouldLogHTTP(rawURL) {
		return
	}
	c.logger(ctx, "gopay app http request", map[string]any{
		"method":  strings.ToUpper(method),
		"url":     rawURL,
		"headers": debugHeaders(headers),
		"body":    debugBody(body),
	})
}

func (c *Client) logHTTPResponse(ctx context.Context, method, rawURL string, resp *protocol.Response, err error) {
	if !c.shouldLogHTTP(rawURL) {
		return
	}
	fields := map[string]any{
		"method": strings.ToUpper(method),
		"url":    rawURL,
	}
	if resp != nil {
		fields["status"] = resp.StatusCode
		fields["headers"] = debugHeaders(resp.Header)
		fields["body"] = debugBody(resp.Body)
	}
	if err != nil {
		fields["error"] = err.Error()
	}
	c.logger(ctx, "gopay app http response", fields)
}

func (c *Client) shouldLogHTTP(rawURL string) bool {
	if !c.debugHTTP || c.logger == nil {
		return false
	}
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return false
	}
	host := strings.ToLower(parsed.Host)
	path := parsed.Path
	if host == "accounts.goto-products.com" {
		switch path {
		case "/goto-auth/login/methods", "/cvs/v1/methods", "/cvs/v1/initiate", "/cvs/v1/verify", "/goto-auth/accountlist", "/goto-auth/token":
			return true
		}
	}
	if host == "api.gojekapi.com" && path == "/v7/customers/signup" {
		return true
	}
	if host == "customer.gopayapi.com" && strings.Contains(path, "/pin") {
		return true
	}
	if host == "customer.gopayapi.com" && path == "/v1/support/customer/initiate" {
		return true
	}
	return false
}

func debugHeaders(headers http.Header) map[string]any {
	out := make(map[string]any, len(headers))
	for key, values := range headers {
		if sensitiveHeader(key) {
			out[key] = "<redacted>"
			continue
		}
		if len(values) == 1 {
			out[key] = values[0]
		} else {
			out[key] = append([]string(nil), values...)
		}
	}
	return out
}

func debugBody(raw []byte) any {
	if len(raw) == 0 {
		return nil
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		return string(raw)
	}
	return redactDebugJSON(value)
}

func redactDebugJSON(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		out := make(map[string]any, len(typed))
		for key, item := range typed {
			if sensitiveJSONKey(key) {
				out[key] = "<redacted>"
			} else {
				out[key] = redactDebugJSON(item)
			}
		}
		return out
	case []any:
		out := make([]any, 0, len(typed))
		for _, item := range typed {
			out = append(out, redactDebugJSON(item))
		}
		return out
	default:
		return typed
	}
}

func sensitiveHeader(key string) bool {
	key = strings.ToLower(strings.TrimSpace(key))
	switch key {
	case "authorization", "cookie", "set-cookie", "proxy-authorization", "verification-token", "x-csrf-token", "x-device-token", "x-devicetoken", "x-imei":
		return true
	default:
		return false
	}
}

func sensitiveJSONKey(key string) bool {
	key = strings.ToLower(strings.TrimSpace(key))
	if key == "otp" || key == "pin" || key == "password" || key == "cookie" || key == "authorization" || key == "api_key" ||
		key == "support_lang" || key == "support_code" || key == "support_id" {
		return true
	}
	return strings.Contains(key, "secret") || strings.Contains(key, "token")
}
