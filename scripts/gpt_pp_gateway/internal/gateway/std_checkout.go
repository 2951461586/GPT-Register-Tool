package gateway

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net"
	stdhttp "net/http"
	"net/url"
	"os/exec"
	"strings"
	"time"

	"golang.org/x/net/proxy"
)

func (e *Extractor) createCheckoutStd(ctx context.Context, token, proxyURL string) (string, string, error) {
	client, err := e.newStdHTTP1Client(proxyURL, e.Config.Timeout)
	if err != nil {
		return "", "", err
	}
	payload := map[string]any{
		"entry_point": "all_plans_pricing_modal",
		"plan_name":   "chatgptplusplan",
		"billing_details": map[string]string{
			"country":  e.Config.Country,
			"currency": e.Config.Currency,
		},
		"promo_campaign":   map[string]any{"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": false},
		"checkout_ui_mode": "hosted",
	}
	body, _ := json.Marshal(payload)
	req, err := stdhttp.NewRequestWithContext(ctx, stdhttp.MethodPost, strings.TrimRight(e.Config.ChatGPTBase, "/")+"/backend-api/payments/checkout", bytes.NewReader(body))
	if err != nil {
		return "", "", err
	}
	deviceID := browserDeviceID(ctx)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Origin", "https://chatgpt.com")
	req.Header.Set("Referer", "https://chatgpt.com/")
	req.Header.Set("X-OpenAI-Target-Path", "/backend-api/payments/checkout")
	req.Header.Set("X-OpenAI-Target-Route", "/backend-api/payments/checkout")
	req.Header.Set("OAI-Device-ID", deviceID)
	req.Header.Set("OAI-Language", "en-US")
	req.Header.Set("Cookie", "oai-did="+deviceID)
	req.Header.Set("User-Agent", stripeAPIUserAgent)
	resp, err := client.Do(req)
	if err != nil {
		return "", "", classifyNetworkError("checkout_network_error", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var data any
	_ = json.Unmarshal(raw, &data)
	hosted := findNestedURL(data, payOpenAIRe)
	if hosted == "" {
		hosted = findNestedURL(data, urlReAnyCheckout)
	}
	if hosted == "" {
		hosted = hostedFromCheckoutObject(data)
	}
	checkoutPK := ""
	if obj, ok := data.(map[string]any); ok {
		checkoutPK = stringValue(firstNonEmpty(obj["publishable_key"], obj["stripe_publishable_key"]))
		if checkoutPK == "" && strings.HasPrefix(stringValue(firstNonEmpty(obj["checkout_session_id"], obj["id"])), "cs_live_") {
			checkoutPK = defaultOpenAIStripePK
		}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 || hosted == "" {
		return "", "", checkoutFailure(resp.StatusCode, resp.Header, raw, hosted)
	}
	return hosted, checkoutPK, nil
}

func hostedFromCheckoutObject(data any) string {
	obj, ok := data.(map[string]any)
	if !ok {
		return ""
	}
	for _, key := range []string{"stripe_hosted_url", "url", "hosted_checkout_url", "checkout_url"} {
		if raw := stringValue(obj[key]); strings.Contains(raw, "/c/pay/") {
			return raw
		}
	}
	cs := stringValue(obj["checkout_session_id"])
	if cs == "" {
		cs = stringValue(obj["id"])
	}
	if !strings.HasPrefix(cs, "cs_") {
		return ""
	}
	secret := stringValue(obj["client_secret"])
	fragment := ""
	if marker := "_secret_"; strings.Contains(secret, marker) {
		fragment = strings.SplitN(secret, marker, 2)[1]
	}
	if fragment != "" {
		return "https://checkout.stripe.com/c/pay/" + cs + "#" + fragment
	}
	return "https://checkout.stripe.com/c/pay/" + cs
}

func (e *Extractor) createCheckoutCurl(ctx context.Context, token, proxyURL string) (string, string, error) {
	payload := map[string]any{
		"entry_point": "all_plans_pricing_modal",
		"plan_name":   "chatgptplusplan",
		"billing_details": map[string]string{
			"country":  e.Config.Country,
			"currency": e.Config.Currency,
		},
		"promo_campaign":   map[string]any{"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": false},
		"checkout_ui_mode": "hosted",
	}
	body, _ := json.Marshal(payload)
	args := []string{
		"-sS", "--http1.1", "-i",
		"--max-time", fmt.Sprintf("%d", int(e.Config.Timeout.Seconds())),
		"--connect-timeout", "8",
	}
	args = append(args, curlProxyArgs(proxyURL)...)
	args = append(args,
		strings.TrimRight(e.Config.ChatGPTBase, "/")+"/backend-api/payments/checkout",
		"-H", "Authorization: Bearer "+token,
		"-H", "Content-Type: application/json",
		"-H", "Accept: application/json",
		"-H", "Origin: https://chatgpt.com",
		"-H", "Referer: https://chatgpt.com/",
		"-H", "X-OpenAI-Target-Path: /backend-api/payments/checkout",
		"-H", "X-OpenAI-Target-Route: /backend-api/payments/checkout",
		"-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
		"--data", string(body),
	)
	out, err := exec.CommandContext(ctx, "curl", args...).Output()
	if err != nil {
		return "", "", classifyNetworkError("checkout_network_error", err)
	}
	respBody := out
	headerText := ""
	if parts := bytes.Split(out, []byte("\r\n\r\n")); len(parts) > 1 {
		respBody = parts[len(parts)-1]
		headerText = string(parts[len(parts)-2])
	}
	var data any
	if err := json.Unmarshal(respBody, &data); err != nil {
		return "", "", &APIError{Code: "checkout_failed", Message: "ChatGPT checkout failed: non-json response " + checkoutHeaderSummary(headerText), Status: 502}
	}
	hosted := findNestedURL(data, payOpenAIRe)
	if hosted == "" {
		hosted = findNestedURL(data, urlReAnyCheckout)
	}
	checkoutPK := ""
	if obj, ok := data.(map[string]any); ok {
		checkoutPK = stringValue(firstNonEmpty(obj["publishable_key"], obj["stripe_publishable_key"]))
		if checkoutPK == "" && strings.HasPrefix(stringValue(firstNonEmpty(obj["checkout_session_id"], obj["id"])), "cs_live_") {
			checkoutPK = defaultOpenAIStripePK
		}
	}
	if hosted == "" {
		return "", "", &APIError{Code: "checkout_failed", Message: "ChatGPT checkout failed: no hosted URL", Status: 502}
	}
	return hosted, checkoutPK, nil
}

func checkoutHeaderSummary(headerText string) string {
	lines := strings.Split(headerText, "\n")
	status := ""
	contentType := ""
	for _, line := range lines {
		line = strings.TrimSpace(line)
		low := strings.ToLower(line)
		if strings.HasPrefix(line, "HTTP/") {
			status = line
		}
		if strings.HasPrefix(low, "content-type:") {
			contentType = line
		}
	}
	if status == "" && contentType == "" {
		return ""
	}
	return strings.TrimSpace(status + " " + contentType)
}

func curlProxyArgs(proxyURL string) []string {
	if proxyURL == "" {
		return nil
	}
	u, err := url.Parse(proxyURL)
	if err != nil {
		return nil
	}
	raw := u.Host
	if u.User != nil {
		raw = u.User.String() + "@" + u.Host
	}
	switch u.Scheme {
	case "socks5h":
		return []string{"--socks5-hostname", raw}
	case "socks5":
		return []string{"--socks5", raw}
	case "http", "https":
		return []string{"--proxy", proxyURL}
	default:
		return nil
	}
}

func (e *Extractor) newStdHTTP1Client(proxyURL string, timeout time.Duration) (*stdhttp.Client, error) {
	tr := &stdhttp.Transport{
		Proxy:                 nil,
		ForceAttemptHTTP2:     false,
		TLSNextProto:          map[string]func(string, *tls.Conn) stdhttp.RoundTripper{},
		MaxIdleConns:          0,
		DisableKeepAlives:     true,
		TLSHandshakeTimeout:   8 * time.Second,
		ResponseHeaderTimeout: timeout,
		ExpectContinueTimeout: 1 * time.Second,
	}
	if proxyURL != "" {
		u, err := url.Parse(proxyURL)
		if err != nil {
			return nil, err
		}
		switch u.Scheme {
		case "http", "https":
			tr.Proxy = stdhttp.ProxyURL(u)
		case "socks5", "socks5h":
			var auth *proxy.Auth
			if u.User != nil {
				pass, _ := u.User.Password()
				auth = &proxy.Auth{User: u.User.Username(), Password: pass}
			}
			dialer, err := proxy.SOCKS5("tcp", u.Host, auth, proxy.Direct)
			if err != nil {
				return nil, err
			}
			tr.DialContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
				ch := make(chan struct {
					conn net.Conn
					err  error
				}, 1)
				go func() {
					conn, err := dialer.Dial(network, addr)
					ch <- struct {
						conn net.Conn
						err  error
					}{conn, err}
				}()
				select {
				case <-ctx.Done():
					return nil, ctx.Err()
				case res := <-ch:
					return res.conn, res.err
				}
			}
		default:
			return nil, &APIError{Code: "invalid_proxy", Message: "代理协议不正确", Status: 400}
		}
	}
	return &stdhttp.Client{
		Transport: tr,
		Timeout:   timeout,
		CheckRedirect: func(req *stdhttp.Request, via []*stdhttp.Request) error {
			return stdhttp.ErrUseLastResponse
		},
	}, nil
}
