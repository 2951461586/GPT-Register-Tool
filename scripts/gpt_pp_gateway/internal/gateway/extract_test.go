package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	stdhttp "net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	http "github.com/bogdanfinn/fhttp"
)

const testToken = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0In0.signature"

func TestExtractHappyPath(t *testing.T) {
	var chatCalls, initCalls, confirmCalls int
	chatBase := "https://chat.local"
	stripeBase := "https://stripe.local"
	client := &fakeHTTPDoer{handle: func(r *http.Request) (*http.Response, error) {
		switch {
		case r.URL.Host == "chat.local" && r.URL.Path == "/backend-api/payments/checkout":
			chatCalls++
			return jsonHTTPResponse(200, map[string]any{"url": stripeBase + "/c/pay/cs_test_1234567890"}), nil
		case r.URL.Host == "stripe.local" && r.URL.Path == "/c/pay/cs_test_1234567890":
			return textHTTPResponse(200, `window.__pk="pk_live_wrong"; window.__pk2="pk_test_abcdefghijklmnopqrstuvwxyz1234567890"`), nil
		case r.URL.Host == "stripe.local" && r.URL.Path == "/v1/payment_pages/cs_test_1234567890/init":
			initCalls++
			form := readFHTTPForm(t, r)
			if form.Get("key") == "" {
				t.Fatal("missing key")
			}
			return jsonHTTPResponse(200, zeroInit(stripeBase+"/c/pay/cs_test_1234567890")), nil
		case r.URL.Host == "stripe.local" && r.URL.Path == "/v1/payment_pages/allowed_origins":
			return jsonHTTPResponse(200, map[string]any{"allowed_origins": []string{"https://pay.openai.com"}}), nil
		case r.URL.Host == "stripe.local" && r.URL.Path == "/v1/elements/sessions":
			return jsonHTTPResponse(200, map[string]any{"status": "open"}), nil
		case r.URL.Host == "stripe.local" && r.URL.Path == "/v1/payment_pages/cs_test_1234567890":
			return jsonHTTPResponse(200, map[string]any{"status": "open"}), nil
		case r.URL.Host == "stripe.local" && r.URL.Path == "/v1/payment_pages/cs_test_1234567890/confirm":
			confirmCalls++
			body := readFHTTPForm(t, r)
			if body.Get("expected_amount") != "0" || body.Get("payment_method_data[type]") != "paypal" {
				t.Fatalf("bad confirm form: %s", body.Encode())
			}
			return jsonHTTPResponse(200, map[string]any{"next_action": map[string]any{"redirect_to_url": map[string]string{"url": "https://pm-redirects.stripe.com/authorize/test/value"}}}), nil
		default:
			return nil, fmt.Errorf("unexpected request: %s %s", r.Method, r.URL.String())
		}
	}}

	extractor := NewExtractor(Config{ChatGPTBase: chatBase, StripeBase: stripeBase, Timeout: 5 * time.Second, AllowNonZero: false})
	hosted, checkoutPK, err := extractor.createCheckout(context.Background(), client, testToken)
	if err != nil {
		t.Fatal(err)
	}
	pk, cs, init, err := extractor.fetchCheckoutInit(context.Background(), client, hosted, checkoutPK)
	if err != nil {
		t.Fatal(err)
	}
	init["hosted_checkout_url"] = hosted
	gate, err := extractor.verifyAmount(init)
	if err != nil {
		t.Fatal(err)
	}
	pmURL, err := extractor.confirmPayPal(context.Background(), client, pk, cs, init)
	if err != nil {
		t.Fatal(err)
	}
	if gate.AmountDue == nil || *gate.AmountDue != 0 || pmURL == "" {
		t.Fatalf("bad result amount=%v paypal=%q", gate.AmountDue, pmURL)
	}
	if chatCalls != 1 || initCalls != 1 || confirmCalls != 1 {
		t.Fatalf("calls chat=%d init=%d confirm=%d", chatCalls, initCalls, confirmCalls)
	}
}

func TestExtractBlocksNonZeroBeforeConfirm(t *testing.T) {
	var confirmCalls int
	chatBase := "https://chat.local"
	stripeBase := "https://stripe.local"
	client := &fakeHTTPDoer{handle: func(r *http.Request) (*http.Response, error) {
		switch {
		case r.URL.Host == "chat.local" && r.URL.Path == "/backend-api/payments/checkout":
			return jsonHTTPResponse(200, map[string]any{"url": stripeBase + "/c/pay/cs_test_abcdef123456"}), nil
		case r.URL.Host == "stripe.local" && r.URL.Path == "/c/pay/cs_test_abcdef123456":
			return textHTTPResponse(200, `pk_test_abcdefghijklmnopqrstuvwxyz1234567890`), nil
		case r.URL.Host == "stripe.local" && r.URL.Path == "/v1/payment_pages/cs_test_abcdef123456/init":
			init := zeroInit(stripeBase + "/c/pay/cs_test_abcdef123456")
			init["invoice"].(map[string]any)["amount_due"] = 1000
			init["total_summary"].(map[string]any)["due"] = 1000
			return jsonHTTPResponse(200, init), nil
		case strings.Contains(r.URL.Path, "/confirm"):
			confirmCalls++
			return jsonHTTPResponse(200, map[string]any{}), nil
		default:
			return nil, fmt.Errorf("unexpected request: %s %s", r.Method, r.URL.String())
		}
	}}

	extractor := NewExtractor(Config{ChatGPTBase: chatBase, StripeBase: stripeBase, Timeout: 5 * time.Second})
	hosted, checkoutPK, err := extractor.createCheckout(context.Background(), client, testToken)
	if err != nil {
		t.Fatal(err)
	}
	_, _, init, err := extractor.fetchCheckoutInit(context.Background(), client, hosted, checkoutPK)
	if err != nil {
		t.Fatal(err)
	}
	gate, err := extractor.verifyAmount(init)
	if err == nil {
		t.Fatal("expected non-zero error")
	}
	if codeOf(err) != "non_zero_amount" || gate == nil || gate.AmountDue == nil || *gate.AmountDue != 1000 {
		t.Fatalf("bad result: gate=%#v err=%v", gate, err)
	}
	if confirmCalls != 0 {
		t.Fatalf("confirm should not be called, got %d", confirmCalls)
	}
}

func TestPrepareTaxRegionFallsBackToUSOnlyAfterPrimaryFailure(t *testing.T) {
	var taxCountries []string
	client := &fakeHTTPDoer{handle: func(r *http.Request) (*http.Response, error) {
		switch {
		case r.URL.Path == "/v1/payment_pages/allowed_origins":
			return jsonHTTPResponse(200, map[string]any{"allowed_origins": []string{"https://pay.openai.com"}}), nil
		case r.URL.Path == "/v1/elements/sessions":
			return jsonHTTPResponse(200, map[string]any{"status": "open"}), nil
		case r.URL.Path == "/v1/payment_pages/cs_test_tax":
			form := readFHTTPForm(t, r)
			country := form.Get("tax_region[country]")
			taxCountries = append(taxCountries, country)
			if country == "DE" {
				return jsonHTTPResponse(400, map[string]any{"error": map[string]any{"message": "blocked"}}), nil
			}
			return jsonHTTPResponse(200, map[string]any{"status": "open", "init_checksum": "us-checksum"}), nil
		default:
			return nil, fmt.Errorf("unexpected request: %s %s", r.Method, r.URL.String())
		}
	}}
	extractor := NewExtractor(Config{StripeBase: "https://stripe.local", Country: "DE", Timeout: 5 * time.Second})
	init, _ := extractor.prepareStripePayPal(context.Background(), client, "pk_test_key", "cs_test_tax", "https://stripe.local/c/pay/cs_test_tax", zeroInit("https://stripe.local/c/pay/cs_test_tax"))
	if got := strings.Join(taxCountries, ","); got != "DE,US" {
		t.Fatalf("tax country fallback = %s, want DE,US", got)
	}
	if init["init_checksum"] != "us-checksum" {
		t.Fatalf("US tax payload was not merged: %#v", init)
	}
}

func TestConfirmPrefersJPThenFallsBackToUS(t *testing.T) {
	var countries []string
	client := &fakeHTTPDoer{handle: func(r *http.Request) (*http.Response, error) {
		if r.URL.Path != "/v1/payment_pages/cs_test_confirm/confirm" {
			return nil, fmt.Errorf("unexpected request: %s %s", r.Method, r.URL.String())
		}
		form := readFHTTPForm(t, r)
		country := form.Get("payment_method_data[billing_details][address][country]")
		countries = append(countries, country)
		if country != "US" {
			return jsonHTTPResponse(200, map[string]any{"status": "open"}), nil
		}
		return jsonHTTPResponse(200, map[string]any{"next_action": map[string]any{"redirect_to_url": map[string]string{"url": "https://pm-redirects.stripe.com/authorize/test/us"}}}), nil
	}}
	extractor := NewExtractor(Config{StripeBase: "https://stripe.local", Country: "DE", Timeout: 5 * time.Second})
	pmURL, err := extractor.confirmPayPal(context.Background(), client, "pk_test_key", "cs_test_confirm", zeroInit("https://stripe.local/c/pay/cs_test_confirm"))
	if err != nil {
		t.Fatal(err)
	}
	if pmURL == "" {
		t.Fatal("missing PayPal URL")
	}
	if got := strings.Join(countries, ","); got != "JP,DE,US" {
		t.Fatalf("confirm countries = %s, want JP,DE,US", got)
	}
}

func TestProxyIPProbeFailureDoesNotBlockExtraction(t *testing.T) {
	var attempts int
	extractor := NewExtractor(Config{MaxAttempts: 3, Timeout: 5 * time.Second})
	extractor.probeExitIPHook = func(context.Context, string) (string, error) {
		return "", &APIError{Code: "proxy_ip_probe_network", Message: "probe down", Status: 502}
	}
	extractor.extractOnceHook = func(context.Context, string, string) (*ExtractResult, error) {
		attempts++
		return &ExtractResult{
			OK:                 true,
			Code:               "paypal_authorize_extracted",
			AmountDisplay:      "0.00 EUR",
			PayPalAuthorizeURL: "https://pm-redirects.stripe.com/authorize/test/value",
		}, nil
	}
	res, err := extractor.extractWithProxy(context.Background(), testToken, "socks5h://proxy.local:1000")
	if err != nil {
		t.Fatal(err)
	}
	if attempts < 1 || attempts > 3 {
		t.Fatalf("attempts=%d, want 1..3", attempts)
	}
	if res == nil || !res.OK || res.PayPalAuthorizeURL == "" {
		t.Fatalf("bad result: %#v", res)
	}
}

func TestProxyDNSErrorClassification(t *testing.T) {
	err := fmt.Errorf("Post %q: socks connect tcp gate-jp.kookeey.info:1000->chatgpt.com:443: dial tcp: lookup gate-jp.kookeey.info: no such host", "https://chatgpt.com/backend-api/payments/checkout")
	if codeOf(err) != "proxy_dns_error" {
		t.Fatalf("codeOf=%s", codeOf(err))
	}
}

func TestSelectPublishableKeyMatchesSessionMode(t *testing.T) {
	html := `pk_test_abcdefghijklmnopqrstuvwxyz pk_live_abcdefghijklmnopqrstuvwxyz`
	if got := selectPublishableKey(html, true); !strings.HasPrefix(got, "pk_live_") {
		t.Fatalf("live selected %q", got)
	}
	if got := selectPublishableKey(html, false); !strings.HasPrefix(got, "pk_test_") {
		t.Fatalf("test selected %q", got)
	}
}

func TestFirstUsefulRedirectURLRejectsApplePayHook(t *testing.T) {
	raw := []byte(`{
		"next_action": {
			"url": "https://pm-hooks.stripe.com/apple_pay/merchant_token/pDq7tf9uieoQWMVJixFwuOve/acct_1Pj377KslHRdbaPg/"
		}
	}`)
	if got := firstUsefulRedirectURL(raw); got != "" {
		t.Fatalf("apple pay hook must not be returned as PayPal authorize URL: %s", got)
	}
}

func TestFirstUsefulRedirectURLAcceptsPayPalAuthorize(t *testing.T) {
	raw := []byte(`{"next_action":{"redirect_to_url":{"url":"https://pm-redirects.stripe.com/authorize/live/paypal_token"}}}`)
	if got := firstUsefulRedirectURL(raw); got != "https://pm-redirects.stripe.com/authorize/live/paypal_token" {
		t.Fatalf("unexpected authorize URL: %s", got)
	}
}

func TestServerExtractCompatibility(t *testing.T) {
	extractor := NewExtractor(Config{Timeout: 5 * time.Second})
	server := NewServer(extractor)
	mux := stdhttp.NewServeMux()
	server.Register(mux)
	req := httptest.NewRequest(stdhttp.MethodPost, "/api/test-proxy", strings.NewReader(`{"proxy":"user:pass@gate-jp.kookeey.info:1000"}`))
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	if rec.Code != stdhttp.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"runtime":"go"`) {
		t.Fatalf("unexpected body: %s", rec.Body.String())
	}
}

func TestConfirmPayloadUsesBillingCycleAnchorAmountField(t *testing.T) {
	init := zeroInit("https://stripe.local/c/pay/cs_test_confirm")
	init["invoice"] = map[string]any{
		"amount_due":           2000,
		"currency":             "usd",
		"billing_cycle_anchor": "2026-07-04T00:00:00Z",
		"has_prorations":       false,
	}
	extractor := NewExtractor(Config{StripeBase: "https://stripe.local", Country: "US", Timeout: 5 * time.Second})
	form := extractor.payPalConfirmForm("pk_test_key", "cs_test_confirm", init, displayAmounts(init), 2000, "https://stripe.local/c/pay/cs_test_confirm", "https://stripe.local/c/pay/cs_test_confirm", "US")
	if got := form.Get("expected_amount"); got != "0" {
		t.Fatalf("expected_amount=%q want 0", got)
	}
	if got := form.Get("expected_amount_on_bca"); got != "2000" {
		t.Fatalf("expected_amount_on_bca=%q want 2000", got)
	}
}

func TestConfirmPayloadUsesInitReturnURLModeAndCheckoutReferrer(t *testing.T) {
	init := zeroInit("https://pay.openai.com/c/pay/cs_test_confirm#secret")
	init["return_url"] = "https://chatgpt.com/checkout/verify?stripe_session_id=cs_test_confirm"
	init["hosted_checkout_url"] = "https://pay.openai.com/c/pay/cs_test_confirm#secret"
	init["stripe_hosted_url"] = "https://checkout.stripe.com/c/pay/cs_test_confirm"
	init["ui_mode"] = "custom"
	extractor := NewExtractor(Config{StripeBase: "https://stripe.local", Country: "US", Timeout: 5 * time.Second})
	returnURL := confirmReturnURLForInit(init, "cs_test_confirm")
	referrer := checkoutReferrerURL(init, "cs_test_confirm")
	form := extractor.payPalConfirmForm("pk_test_key", "cs_test_confirm", init, displayAmounts(init), 2000, returnURL, referrer, "US")
	if got := form.Get("return_url"); !strings.HasPrefix(got, "https://pay.openai.com/c/pay/cs_test_confirm?") {
		t.Fatalf("return_url=%q does not use checkout URL", got)
	}
	if got := form.Get("return_url"); !strings.Contains(got, "redirect_pm_type=paypal") || !strings.Contains(got, "ui_mode=custom") {
		t.Fatalf("return_url missing Stripe redirect params: %q", got)
	}
	if got := form.Get("referrer"); got != "https://pay.openai.com/c/pay/cs_test_confirm" {
		t.Fatalf("referrer=%q", got)
	}
}

func TestDisplayAmountsUsesTaxBreakdownSeparately(t *testing.T) {
	init := zeroInit("https://stripe.local/c/pay/cs_test_confirm")
	init["invoice"] = map[string]any{
		"amount_due": 2150,
		"currency":   "usd",
		"subtotal":   2000,
		"total":      2150,
		"total_tax_amounts": []any{
			map[string]any{"amount": 150, "tax_behavior": "exclusive"},
			map[string]any{"amount": 25, "tax_behavior": "inclusive"},
		},
	}
	init["total_summary"] = map[string]any{"subtotal": 2000, "total": 2150, "due": 2150}
	amounts := displayAmounts(init)
	if amounts["total_exclusive_tax"] != 150 {
		t.Fatalf("exclusive tax=%d", amounts["total_exclusive_tax"])
	}
	if amounts["total_inclusive_tax"] != 25 {
		t.Fatalf("inclusive tax=%d", amounts["total_inclusive_tax"])
	}
}

func TestExtractProxyPoolSerializesCheckoutCandidates(t *testing.T) {
	var calls atomic.Int32
	extractor := NewExtractor(Config{MaxAttempts: 1, RaceParallel: 2, Timeout: 1 * time.Second})
	extractor.extractOnceHook = func(ctx context.Context, token, proxy string) (*ExtractResult, error) {
		call := calls.Add(1)
		if call == 1 {
			time.Sleep(120 * time.Millisecond)
			return &ExtractResult{OK: false, Code: "stripe_confirm_failed", AmountDisplay: "unknown"}, &APIError{Code: "stripe_confirm_failed", Message: "first proxy miss", Status: 502}
		}
		return &ExtractResult{
			OK:                 true,
			Code:               "paypal_authorize_extracted",
			AmountDisplay:      "0.00 USD",
			PayPalAuthorizeURL: "https://pm-redirects.stripe.com/authorize/test/fast",
		}, nil
	}
	start := time.Now()
	res, err := extractor.Extract(context.Background(), testToken, "user:pass@gate-jp.kookeey.info:1000\nuser:pass@gate-us.kookeey.info:1000")
	if err != nil {
		t.Fatal(err)
	}
	if res == nil || !res.OK || res.PayPalAuthorizeURL == "" {
		t.Fatalf("bad result: %#v", res)
	}
	if calls.Load() != 2 {
		t.Fatalf("checkout candidates should be tried in order, calls=%d", calls.Load())
	}
	if elapsed := time.Since(start); elapsed < 100*time.Millisecond {
		t.Fatalf("checkout candidates ran in parallel unexpectedly: %s", elapsed)
	}
}

func TestBrowserHeadersReuseDeviceIDFromContext(t *testing.T) {
	ctx := contextWithBrowserSession(context.Background())
	req1, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://chatgpt.com/", nil)
	if err != nil {
		t.Fatal(err)
	}
	req2, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://chatgpt.com/backend-api/payments/checkout/approve", nil)
	if err != nil {
		t.Fatal(err)
	}
	setBrowserHeaders(req1)
	setBrowserHeaders(req2)
	deviceID := req1.Header.Get("Oai-Device-Id")
	if deviceID == "" {
		t.Fatal("missing device id")
	}
	if req2.Header.Get("Oai-Device-Id") != deviceID {
		t.Fatalf("device id changed: %q != %q", req2.Header.Get("Oai-Device-Id"), deviceID)
	}
	if req2.Header.Get("Cookie") != "oai-did="+deviceID {
		t.Fatalf("cookie not tied to device id: %q", req2.Header.Get("Cookie"))
	}
}

func TestExtractRaceParentCancelDoesNotCancelStartedRacer(t *testing.T) {
	var calls atomic.Int32
	started := make(chan struct{})
	extractor := NewExtractor(Config{MaxAttempts: 1, RaceParallel: 1, Timeout: time.Second})
	extractor.extractOnceHook = func(ctx context.Context, token, proxy string) (*ExtractResult, error) {
		calls.Add(1)
		close(started)
		time.Sleep(80 * time.Millisecond)
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		return &ExtractResult{
			OK:                 true,
			Code:               "paypal_authorize_extracted",
			AmountDisplay:      "0.00 USD",
			PayPalAuthorizeURL: "https://pm-redirects.stripe.com/authorize/test/no-shared-cancel",
		}, nil
	}
	parent, cancel := context.WithCancel(context.Background())
	type done struct {
		res *ExtractResult
		err error
	}
	doneCh := make(chan done, 1)
	go func() {
		res, err := extractor.Extract(parent, testToken, "user:pass@gate-jp.kookeey.info:1000")
		doneCh <- done{res: res, err: err}
	}()
	select {
	case <-started:
		cancel()
	case <-time.After(time.Second):
		t.Fatal("racer did not start")
	}
	var got done
	select {
	case got = <-doneCh:
	case <-time.After(time.Second):
		t.Fatal("extract did not return")
	}
	res, err := got.res, got.err
	if err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 1 || res == nil || !res.OK {
		t.Fatalf("racer was cancelled/shared unexpectedly calls=%d res=%#v", calls.Load(), res)
	}
}

func TestServerBatchExtractStreamsResults(t *testing.T) {
	token2 := "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0MiJ9.signature"
	extractor := NewExtractor(Config{MaxAttempts: 1, RaceParallel: 1, Timeout: 1 * time.Second})
	extractor.extractOnceHook = func(ctx context.Context, token, proxy string) (*ExtractResult, error) {
		return &ExtractResult{
			OK:                 true,
			Code:               "paypal_authorize_extracted",
			AmountDisplay:      "0.00 USD",
			PayPalAuthorizeURL: "https://pm-redirects.stripe.com/authorize/test/" + token[len(token)-9:],
		}, nil
	}
	server := NewServer(extractor)
	mux := stdhttp.NewServeMux()
	server.Register(mux)
	body := fmt.Sprintf(`{"tokens":["%s","%s"],"proxy":"user:pass@gate-jp.kookeey.info:1000"}`, testToken, token2)
	req := httptest.NewRequest(stdhttp.MethodPost, "/api/extract-batch", strings.NewReader(body))
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	if rec.Code != stdhttp.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	lines := strings.Split(strings.TrimSpace(rec.Body.String()), "\n")
	if len(lines) != 2 {
		t.Fatalf("lines=%d body=%s", len(lines), rec.Body.String())
	}
	for _, line := range lines {
		if !strings.Contains(line, `"ok":true`) || !strings.Contains(line, "pm-redirects.stripe.com/authorize") {
			t.Fatalf("bad line: %s", line)
		}
	}
}

func TestServerBatchRejectsMissingProxy(t *testing.T) {
	extractor := NewExtractor(Config{MaxAttempts: 1, RaceParallel: 1, Timeout: time.Second})
	server := NewServer(extractor)
	mux := stdhttp.NewServeMux()
	server.Register(mux)
	body := fmt.Sprintf(`{"tokens":["%s"]}`, testToken)
	req := httptest.NewRequest(stdhttp.MethodPost, "/api/extract-batch", strings.NewReader(body))
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	if rec.Code != stdhttp.StatusBadRequest {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "missing_proxy") {
		t.Fatalf("missing proxy was not enforced: %s", rec.Body.String())
	}
}

func TestServerBatchAcceptsSessionJSONOnlyAccessToken(t *testing.T) {
	var seenToken string
	extractor := NewExtractor(Config{MaxAttempts: 1, RaceParallel: 1, Timeout: time.Second})
	extractor.extractOnceHook = func(ctx context.Context, token, proxy string) (*ExtractResult, error) {
		seenToken = token
		return &ExtractResult{
			OK:                 true,
			Code:               "paypal_authorize_extracted",
			AmountDisplay:      "0.00 USD",
			PayPalAuthorizeURL: "https://pm-redirects.stripe.com/authorize/test/session-token",
		}, nil
	}
	server := NewServer(extractor)
	mux := stdhttp.NewServeMux()
	server.Register(mux)
	sessionJSON := fmt.Sprintf(`{"user":{"email":"ignored@example.test"},"session":{"accessToken":"%s"},"rumViewTags":{"light_account":{"fetched":false}}}`, testToken)
	body := fmt.Sprintf(`{"credential":%q,"proxy":"user:pass@gate-jp.kookeey.info:1000"}`, sessionJSON)
	req := httptest.NewRequest(stdhttp.MethodPost, "/api/extract-batch", strings.NewReader(body))
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	if rec.Code != stdhttp.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if seenToken != testToken {
		t.Fatalf("session JSON was not reduced to accessToken: %q", seenToken)
	}
	if !strings.Contains(rec.Body.String(), `"ok":true`) {
		t.Fatalf("unexpected body: %s", rec.Body.String())
	}
}

func zeroInit(returnURL string) map[string]any {
	return map[string]any{
		"url":                  returnURL,
		"init_checksum":        "checksum",
		"customer_email":       "test@example.com",
		"payment_method_types": []any{"card", "paypal"},
		"currency":             "jpy",
		"invoice":              map[string]any{"amount_due": 0, "currency": "jpy"},
		"total_summary":        map[string]any{"subtotal": 0, "total": 0, "due": 0},
	}
}

func writeJSONTest(w stdhttp.ResponseWriter, value any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(value)
}

func readFHTTPForm(t *testing.T, r *http.Request) url.Values {
	t.Helper()
	if r.Body == nil {
		return url.Values{}
	}
	raw, err := io.ReadAll(r.Body)
	if err != nil {
		t.Fatal(err)
	}
	form, err := url.ParseQuery(string(raw))
	if err != nil {
		t.Fatal(err)
	}
	return form
}

type fakeHTTPDoer struct {
	handle func(*http.Request) (*http.Response, error)
}

func (f *fakeHTTPDoer) Do(req *http.Request) (*http.Response, error) {
	return f.handle(req)
}

func (f *fakeHTTPDoer) CloseIdleConnections() {}

func jsonHTTPResponse(status int, value any) *http.Response {
	raw, _ := json.Marshal(value)
	return responseWithBody(status, "application/json", string(raw))
}

func textHTTPResponse(status int, value string) *http.Response {
	return responseWithBody(status, "text/html", value)
}

func responseWithBody(status int, contentType, body string) *http.Response {
	return &http.Response{
		StatusCode: status,
		Status:     fmt.Sprintf("%d", status),
		Header:     http.Header{"Content-Type": []string{contentType}},
		Body:       io.NopCloser(strings.NewReader(body)),
	}
}
