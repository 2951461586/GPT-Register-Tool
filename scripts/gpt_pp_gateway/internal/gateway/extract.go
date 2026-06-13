package gateway

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"mime"
	stdhttp "net/http"
	"net/url"
	"os/exec"
	"regexp"
	"strings"
	"sync"
	"time"

	http "github.com/bogdanfinn/fhttp"
)

var (
	payOpenAIRe      = regexp.MustCompile(`https://pay\.openai\.com/c/pay/[^"'\s<>]+`)
	pmRedirectRe     = regexp.MustCompile(`https://pm-redirects\.stripe\.com/authorize/[^"'\s<>]+`)
	anyURLRe         = regexp.MustCompile(`https://[^"'\s<>\\]+`)
	csRe             = regexp.MustCompile(`(cs_(?:live|test)_[A-Za-z0-9]+)`)
	pkRe             = regexp.MustCompile(`(pk_(?:live|test)_[A-Za-z0-9_]{10,})`)
	urlReAnyCheckout = regexp.MustCompile(`https?://[^"'\s<>]+/c/pay/[^"'\s<>]+`)
	zeroDecimal      = map[string]bool{
		"bif": true, "clp": true, "djf": true, "gnf": true, "jpy": true, "kmf": true,
		"krw": true, "mga": true, "pyg": true, "rwf": true, "ugx": true, "vnd": true,
		"vuv": true, "xaf": true, "xof": true, "xpf": true,
	}
)

const (
	defaultOpenAIStripePK = "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n"
	stripeVersionFull     = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
	stripeRuntimeVersion  = "6f8494a281"
	stripeAPIUserAgent    = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

func (e *Extractor) Extract(ctx context.Context, accessToken, rawProxy string) (*ExtractResult, error) {
	start := time.Now()
	token, err := ExtractAccessToken(accessToken)
	if err != nil {
		return nil, err
	}
	candidates, err := NormalizeProxyCandidates(rawProxy)
	if err != nil {
		return nil, err
	}
	candidates = RotateProxyCandidates(candidates, token)

	res, err := e.extractProxyPool(ctx, token, candidates)
	if res != nil {
		res.ElapsedMS = time.Since(start).Milliseconds()
		return res, err
	}
	if err == nil {
		err = &APIError{Code: "extract_failed", Message: "转链失败", Status: 502}
	}
	return &ExtractResult{OK: false, Code: codeOf(err), Message: err.Error(), AmountDisplay: "unknown", ElapsedMS: time.Since(start).Milliseconds()}, err
}

func isRetryable(err error) bool {
	if err == nil {
		return false
	}
	code := codeOf(err)
	return strings.Contains(code, "network") ||
		code == "checkout_failed" ||
		code == "checkout_session_missing" ||
		code == "stripe_checkout_failed" ||
		code == "stripe_publishable_key_missing" ||
		code == "stripe_init_failed" ||
		code == "stripe_init_invalid_json" ||
		code == "stripe_confirm_failed"
}

func isCheckoutTransportRetryable(err error) bool {
	code := codeOf(err)
	return code == "checkout_network_error" ||
		code == "proxy_connect_rejected" ||
		code == "proxy_dns_error" ||
		code == "checkout_forbidden" ||
		code == "checkout_failed"
}

func (e *Extractor) extractWithProxy(ctx context.Context, token, proxyURL string) (*ExtractResult, error) {
	return e.extractProxyPool(ctx, token, []string{proxyURL})
}

func (e *Extractor) extractProxyPool(ctx context.Context, token string, candidates []string) (*ExtractResult, error) {
	if len(candidates) == 0 {
		err := &APIError{Code: "missing_proxy", Message: "请填写代理", Status: 400}
		return &ExtractResult{OK: false, Code: err.Code, Message: err.Message, AmountDisplay: "unknown"}, err
	}
	attempts := e.Config.MaxAttempts
	if attempts <= 0 {
		attempts = 1
	}
	var lastErr error
	var lastResult *ExtractResult
	for attempt := 1; attempt <= attempts; attempt++ {
		for _, candidate := range candidates {
			select {
			case <-ctx.Done():
				err := &APIError{Code: "client_timeout", Message: ctx.Err().Error(), Status: 504}
				if lastResult != nil {
					return lastResult, preferRaceError(lastErr, err)
				}
				return &ExtractResult{OK: false, Code: err.Code, Message: err.Message, AmountDisplay: "unknown"}, err
			default:
			}
			// Checkout creation is intentionally serialized per account. Creating
			// multiple checkout sessions for the same access token in parallel can
			// invalidate earlier Stripe sessions and produce false "session inactive".
			attemptCtx, cancel := detachedAttemptContext(ctx, e.raceAttemptTimeout())
			start := time.Now()
			res, err := e.extractWithProxyOncePool(attemptCtx, token, candidate, candidates)
			cancel()
			if res != nil {
				res.ProxyScheme = proxyScheme(candidate)
				if res.ElapsedMS == 0 {
					res.ElapsedMS = time.Since(start).Milliseconds()
				}
				lastResult = preferExtractResult(lastResult, res)
			}
			if err == nil && res != nil && res.OK && res.PayPalAuthorizeURL != "" {
				return res, nil
			}
			if err != nil {
				log.Printf("extract proxy-pool miss attempt=%d/%d scheme=%s code=%s elapsed_ms=%d", attempt, attempts, proxyScheme(candidate), codeOf(err), time.Since(start).Milliseconds())
				lastErr = preferRaceError(lastErr, err)
				if isFatalCredentialError(err) {
					if res != nil {
						return res, err
					}
					return &ExtractResult{OK: false, Code: codeOf(err), Message: err.Error(), AmountDisplay: "unknown", ProxyScheme: proxyScheme(candidate)}, err
				}
			}
		}
	}
	if lastErr == nil {
		lastErr = &APIError{Code: "extract_failed", Message: "转链失败", Status: 502}
	}
	if lastResult != nil {
		return lastResult, lastErr
	}
	return &ExtractResult{OK: false, Code: codeOf(lastErr), Message: lastErr.Error(), AmountDisplay: "unknown"}, lastErr
}

func (e *Extractor) extractRace(ctx context.Context, token string, candidates []string) (*ExtractResult, error) {
	if len(candidates) == 0 {
		err := &APIError{Code: "missing_proxy", Message: "请填写代理", Status: 400}
		return &ExtractResult{OK: false, Code: err.Code, Message: err.Message, AmountDisplay: "unknown"}, err
	}
	attempts := e.Config.MaxAttempts
	if attempts <= 0 {
		attempts = 1
	}
	tasks := make([]string, 0, len(candidates)*attempts)
	for _, candidate := range candidates {
		candidate = strings.TrimSpace(candidate)
		for i := 0; i < attempts; i++ {
			tasks = append(tasks, candidate)
		}
	}
	if len(tasks) == 0 {
		err := &APIError{Code: "missing_proxy", Message: "请填写代理", Status: 400}
		return &ExtractResult{OK: false, Code: err.Code, Message: err.Message, AmountDisplay: "unknown"}, err
	}

	parallel := e.Config.RaceParallel
	if parallel <= 0 {
		parallel = attempts
	}
	if parallel > len(tasks) {
		parallel = len(tasks)
	}
	if parallel < 1 {
		parallel = 1
	}

	sem := make(chan struct{}, parallel)
	out := make(chan raceOutcome, len(tasks))
	var wg sync.WaitGroup
	for i, candidate := range tasks {
		wg.Add(1)
		go func(attempt int, candidate string) {
			defer wg.Done()
			select {
			case sem <- struct{}{}:
				defer func() { <-sem }()
			case <-ctx.Done():
				out <- raceOutcome{err: &APIError{Code: "client_timeout", Message: ctx.Err().Error(), Status: 504}, candidate: candidate, attempt: attempt}
				return
			}
			// Every racer gets its own timeout context. It intentionally does
			// not derive from a shared cancellable attempt context: a slow
			// or failing branch must never cancel sibling racers, and a fast
			// success must be returned immediately while losers finish/timeout
			// in the background and are drained from the buffered channel.
			attemptCtx, cancel := detachedAttemptContext(ctx, e.raceAttemptTimeout())
			defer cancel()
			start := time.Now()
			res, err := e.extractWithProxyOnce(attemptCtx, token, candidate)
			if res != nil {
				res.ProxyScheme = proxyScheme(candidate)
				if res.ElapsedMS == 0 {
					res.ElapsedMS = time.Since(start).Milliseconds()
				}
			}
			if err != nil {
				log.Printf("extract race miss attempt=%d/%d scheme=%s code=%s elapsed_ms=%d", attempt, len(tasks), proxyScheme(candidate), codeOf(err), time.Since(start).Milliseconds())
			}
			out <- raceOutcome{result: res, err: err, candidate: candidate, attempt: attempt}
		}(i+1, candidate)
	}
	go func() {
		wg.Wait()
		close(out)
	}()

	var lastErr error
	var lastResult *ExtractResult
	for item := range out {
		if item.result != nil {
			item.result.ProxyScheme = proxyScheme(item.candidate)
			lastResult = preferExtractResult(lastResult, item.result)
		}
		if item.err == nil && item.result != nil && item.result.OK && item.result.PayPalAuthorizeURL != "" {
			go drainRaceChannel(out)
			return item.result, nil
		}
		if item.err != nil {
			lastErr = preferRaceError(lastErr, item.err)
			if isFatalCredentialError(item.err) {
				if item.result != nil {
					item.result.ProxyScheme = proxyScheme(item.candidate)
					go drainRaceChannel(out)
					return item.result, item.err
				}
				go drainRaceChannel(out)
				return &ExtractResult{
					OK:            false,
					Code:          codeOf(item.err),
					Message:       item.err.Error(),
					AmountDisplay: "unknown",
					ProxyScheme:   proxyScheme(item.candidate),
				}, item.err
			}
		}
	}
	if lastErr == nil && ctx.Err() != nil {
		lastErr = &APIError{Code: "client_timeout", Message: ctx.Err().Error(), Status: 504}
	}
	if lastErr == nil {
		lastErr = &APIError{Code: "extract_failed", Message: "转链失败", Status: 502}
	}
	if lastResult != nil {
		return lastResult, lastErr
	}
	return &ExtractResult{OK: false, Code: codeOf(lastErr), Message: lastErr.Error(), AmountDisplay: "unknown"}, lastErr
}

func (e *Extractor) extractSequential(ctx context.Context, token string, candidates []string) (*ExtractResult, error) {
	attempts := e.Config.MaxAttempts
	if attempts <= 0 {
		attempts = 1
	}
	var lastErr error
	var lastResult *ExtractResult
	for attempt := 1; attempt <= attempts; attempt++ {
		for _, candidate := range candidates {
			select {
			case <-ctx.Done():
				err := &APIError{Code: "client_timeout", Message: ctx.Err().Error(), Status: 504}
				if lastResult != nil {
					return lastResult, preferRaceError(lastErr, err)
				}
				return &ExtractResult{OK: false, Code: err.Code, Message: err.Message, AmountDisplay: "unknown"}, err
			default:
			}
			attemptCtx, cancel := detachedAttemptContext(ctx, e.raceAttemptTimeout())
			start := time.Now()
			res, err := e.extractWithProxyOnce(attemptCtx, token, candidate)
			cancel()
			if res != nil {
				res.ProxyScheme = proxyScheme(candidate)
				if res.ElapsedMS == 0 {
					res.ElapsedMS = time.Since(start).Milliseconds()
				}
				lastResult = preferExtractResult(lastResult, res)
			}
			if err == nil && res != nil && res.OK && res.PayPalAuthorizeURL != "" {
				return res, nil
			}
			if err != nil {
				log.Printf("extract sequential miss attempt=%d/%d scheme=%s code=%s elapsed_ms=%d", attempt, attempts, proxyScheme(candidate), codeOf(err), time.Since(start).Milliseconds())
				lastErr = preferRaceError(lastErr, err)
				if isFatalCredentialError(err) {
					if res != nil {
						return res, err
					}
					return &ExtractResult{OK: false, Code: codeOf(err), Message: err.Error(), AmountDisplay: "unknown", ProxyScheme: proxyScheme(candidate)}, err
				}
				if !isRetryable(err) {
					if res != nil {
						return res, err
					}
					return &ExtractResult{OK: false, Code: codeOf(err), Message: err.Error(), AmountDisplay: "unknown", ProxyScheme: proxyScheme(candidate)}, err
				}
			}
		}
	}
	if lastErr == nil {
		lastErr = &APIError{Code: "extract_failed", Message: "转链失败", Status: 502}
	}
	if lastResult != nil {
		return lastResult, lastErr
	}
	return &ExtractResult{OK: false, Code: codeOf(lastErr), Message: lastErr.Error(), AmountDisplay: "unknown"}, lastErr
}

func drainRaceChannel(ch <-chan raceOutcome) {
	for range ch {
	}
}

func (e *Extractor) raceAttemptTimeout() time.Duration {
	base := e.Config.Timeout
	if base <= 0 {
		base = 10 * time.Second
	}
	budget := 3 * base
	if budget < 24*time.Second {
		return 24 * time.Second
	}
	return budget
}

func preferExtractResult(current, next *ExtractResult) *ExtractResult {
	if current == nil {
		return next
	}
	if next == nil {
		return current
	}
	if !next.OK && !current.OK {
		nextRank := failureRank(next.Code)
		currentRank := failureRank(current.Code)
		if nextRank > currentRank {
			return next
		}
		if nextRank < currentRank {
			return current
		}
	}
	if next.AmountDisplay != "" && next.AmountDisplay != "unknown" && (current.AmountDisplay == "" || current.AmountDisplay == "unknown") {
		return next
	}
	if next.HostedCheckoutURL != "" && current.HostedCheckoutURL == "" {
		return next
	}
	return current
}

func preferRaceError(current, next error) error {
	if current == nil {
		return next
	}
	if next == nil {
		return current
	}
	if failureRank(codeOf(next)) > failureRank(codeOf(current)) {
		return next
	}
	return current
}

func isFatalCredentialError(err error) bool {
	switch codeOf(err) {
	case "checkout_token_invalidated", "checkout_unauthorized":
		return true
	default:
		return false
	}
}

func failureRank(code string) int {
	switch code {
	case "checkout_token_invalidated":
		return 100
	case "checkout_unauthorized":
		return 95
	case "checkout_forbidden":
		return 90
	case "checkout_rate_limited":
		return 85
	case "checkout_no_hosted_url":
		return 80
	case "stripe_confirm_failed":
		return 75
	case "stripe_confirm_network_error":
		return 73
	case "stripe_init_failed", "stripe_init_invalid_json":
		return 70
	case "stripe_init_network_error":
		return 69
	case "stripe_publishable_key_missing":
		return 68
	case "stripe_checkout_failed":
		return 65
	case "stripe_checkout_network_error":
		return 64
	case "checkout_failed", "checkout_session_missing":
		return 60
	case "proxy_connect_rejected":
		return 20
	case "proxy_dns_error":
		return 18
	case "checkout_network_error", "stripe_prepare_network_error":
		return 25
	case "client_timeout":
		return 10
	default:
		return 1
	}
}

func (e *Extractor) extractWithProxyOnce(ctx context.Context, token, proxyURL string) (*ExtractResult, error) {
	return e.extractWithProxyOnceWithPool(ctx, token, proxyURL, []string{proxyURL})
}

func (e *Extractor) extractWithProxyOncePool(ctx context.Context, token, proxyURL string, proxyPool []string) (*ExtractResult, error) {
	return e.extractWithProxyOnceWithPool(ctx, token, proxyURL, proxyPool)
}

func (e *Extractor) extractWithProxyOnceWithPool(ctx context.Context, token, proxyURL string, proxyPool []string) (*ExtractResult, error) {
	if e.extractOnceHook != nil {
		return e.extractOnceHook(ctx, token, proxyURL)
	}
	res, err := e.extractWithProxyOnceGo(ctx, token, proxyURL, proxyPool)
	if err == nil && res != nil && res.OK {
		return res, nil
	}
	if proxyURL == "" || !isRetryable(err) || !e.Config.UsePythonFallback {
		return e.finalizeGoError(res, err)
	}
	hosted := ""
	pk := ""
	if res != nil {
		hosted = res.HostedCheckoutURL
		pk = res.PublishableKey
	}
	pyRes, pyErr := e.extractWithPython(ctx, token, proxyURL, hosted, pk)
	if pyErr == nil && pyRes != nil && pyRes.OK {
		return pyRes, nil
	}
	if pyErr != nil {
		log.Printf("python fallback failed code=%s msg=%q", codeOf(pyErr), sanitizeErrorSnippet(pyErr.Error(), 180))
	}
	return e.finalizeGoError(res, err)
}

func (e *Extractor) finalizeGoError(res *ExtractResult, err error) (*ExtractResult, error) {
	if isProxyDNSError(err) {
		err = &APIError{Code: "proxy_dns_error", Message: "代理域名无法解析", Status: 502}
		if res != nil {
			res.Code = "proxy_dns_error"
			res.Message = err.Error()
		}
	}
	return res, err
}

func (e *Extractor) extractWithProxyOnceGo(ctx context.Context, token, proxyURL string, proxyPool []string) (*ExtractResult, error) {
	ctx = contextWithBrowserSession(ctx)
	chatGPTClient, err := e.newHTTPClientWithJar(proxyURL, e.Config.Timeout, nil)
	if err != nil {
		return &ExtractResult{OK: false, Code: codeOf(err), Message: err.Error(), AmountDisplay: "unknown"}, err
	}
	defer chatGPTClient.CloseIdleConnections()
	hosted, checkoutPK, err := e.createCheckout(ctx, chatGPTClient, token)
	if err != nil && isCheckoutTransportRetryable(err) {
		hosted, checkoutPK, err = e.createCheckoutStd(ctx, token, proxyURL)
		if err == nil {
			chatGPTClient, err = e.newHTTPClientWithJar(proxyURL, e.Config.Timeout, nil)
			if err == nil {
				defer chatGPTClient.CloseIdleConnections()
				if warmErr := e.chatGPTPing(ctx, chatGPTClient, token); warmErr != nil {
					log.Printf("checkout session warmup after std fallback failed code=%s msg=%q", codeOf(warmErr), sanitizeErrorSnippet(warmErr.Error(), 160))
				}
			}
		}
	}
	if err != nil {
		return &ExtractResult{OK: false, Code: codeOf(err), Message: err.Error(), AmountDisplay: "unknown"}, err
	}
	providerProxies := providerProxyPool(proxyURL, proxyPool, e.Config.ProxyRotations)
	providerProxy := proxyURL
	if len(providerProxies) > 0 {
		providerProxy = providerProxies[0]
	}
	stripeClient, err := e.newHTTPClient(providerProxy, e.Config.Timeout)
	if err != nil {
		return &ExtractResult{OK: false, Code: codeOf(err), Message: err.Error(), HostedCheckoutURL: hosted, AmountDisplay: "unknown"}, err
	}
	defer stripeClient.CloseIdleConnections()

	pk, cs, init, err := e.fetchCheckoutInit(ctx, stripeClient, hosted, checkoutPK)
	if err != nil {
		return &ExtractResult{OK: false, Code: codeOf(err), Message: err.Error(), HostedCheckoutURL: hosted, PublishableKey: checkoutPK, AmountDisplay: "unknown"}, err
	}
	hosted = effectiveHostedCheckoutURL(init, hosted)
	init["hosted_checkout_url"] = hosted
	gate, err := e.verifyAmount(init)
	if err != nil {
		amount := gate.AmountDue
		return &ExtractResult{OK: false, Code: codeOf(err), Message: err.Error(), HostedCheckoutURL: hosted, PublishableKey: pk, AmountDue: amount, Currency: gate.Currency, AmountDisplay: amountDisplay(amount, gate.Currency)}, err
	}
	if pmURL := existingPayPalAuthorizeURL(init); pmURL != "" {
		amount := gate.AmountDue
		return &ExtractResult{
			OK:                 true,
			Code:               "paypal_authorize_extracted",
			ZeroVerified:       amount != nil && *amount == 0,
			AmountDue:          amount,
			Currency:           gate.Currency,
			AmountDisplay:      amountDisplay(amount, gate.Currency),
			HostedCheckoutURL:  hosted,
			PayPalAuthorizeURL: pmURL,
		}, nil
	}

	if pmURL, err := e.confirmPayPal(ctx, stripeClient, pk, cs, init); err == nil && pmURL != "" {
		amount := gate.AmountDue
		return &ExtractResult{
			OK:                 true,
			Code:               "paypal_authorize_extracted",
			ZeroVerified:       amount != nil && *amount == 0,
			AmountDue:          amount,
			Currency:           gate.Currency,
			AmountDisplay:      amountDisplay(amount, gate.Currency),
			HostedCheckoutURL:  hosted,
			PayPalAuthorizeURL: pmURL,
		}, nil
	} else if err != nil {
		log.Printf("stripe direct confirm failed code=%s msg=%q", codeOf(err), sanitizeErrorSnippet(err.Error(), 180))
	}

	if pmURL, err := e.confirmPayPalWithPaymentMethod(ctx, stripeClient, pk, cs, init); err == nil && pmURL != "" {
		amount := gate.AmountDue
		return &ExtractResult{
			OK:                 true,
			Code:               "paypal_authorize_extracted",
			ZeroVerified:       amount != nil && *amount == 0,
			AmountDue:          amount,
			Currency:           gate.Currency,
			AmountDisplay:      amountDisplay(amount, gate.Currency),
			HostedCheckoutURL:  hosted,
			PayPalAuthorizeURL: pmURL,
		}, nil
	} else if err != nil {
		log.Printf("stripe payment_method confirm failed code=%s msg=%q", codeOf(err), sanitizeErrorSnippet(err.Error(), 180))
	}

	if pmURL, usedProxy, err := e.confirmPayPalCustomAcrossProxies(ctx, chatGPTClient, token, providerProxies, pk, cs, init); err == nil && pmURL != "" {
		amount := gate.AmountDue
		result := &ExtractResult{
			OK:                 true,
			Code:               "paypal_authorize_extracted",
			ZeroVerified:       amount != nil && *amount == 0,
			AmountDue:          amount,
			Currency:           gate.Currency,
			AmountDisplay:      amountDisplay(amount, gate.Currency),
			HostedCheckoutURL:  hosted,
			PayPalAuthorizeURL: pmURL,
		}
		e.attachProxyGeo(ctx, result, usedProxy)
		return result, nil
	} else if err != nil {
		log.Printf("stripe custom confirm failed code=%s msg=%q", codeOf(err), sanitizeErrorSnippet(err.Error(), 180))
	}
	amount := gate.AmountDue
	err = &APIError{Code: "stripe_confirm_failed", Message: "Stripe confirm did not return PayPal authorize URL", Status: 502}
	return &ExtractResult{OK: false, Code: codeOf(err), Message: err.Error(), HostedCheckoutURL: hosted, PublishableKey: pk, AmountDue: amount, Currency: gate.Currency, AmountDisplay: amountDisplay(amount, gate.Currency)}, err
}

func (e *Extractor) extractWithPython(ctx context.Context, token, proxyURL, hosted, pk string) (*ExtractResult, error) {
	pyCtx, cancel := context.WithTimeout(ctx, 22*time.Second)
	defer cancel()
	payload, _ := json.Marshal(map[string]any{
		"access_token":   token,
		"proxy":          proxyURL,
		"country":        e.Config.Country,
		"currency":       e.Config.Currency,
		"allow_non_zero": e.Config.AllowNonZero,
	})
	if hosted != "" {
		var obj map[string]any
		_ = json.Unmarshal(payload, &obj)
		obj["hosted_checkout_url"] = hosted
		if pk != "" {
			obj["publishable_key"] = pk
		}
		payload, _ = json.Marshal(obj)
	}
	cmd := exec.CommandContext(pyCtx, e.Config.PythonBin, e.Config.PythonExecutor)
	cmd.Stdin = bytes.NewReader(payload)
	out, err := cmd.Output()
	if err != nil {
		if exit, ok := err.(*exec.ExitError); ok && len(exit.Stderr) > 0 {
			log.Printf("python fallback stderr=%q", sanitizeErrorSnippet(string(exit.Stderr), 240))
		}
	}
	raw := bytes.TrimSpace(out)
	if lines := bytes.Split(raw, []byte("\n")); len(lines) > 0 {
		raw = bytes.TrimSpace(lines[len(lines)-1])
	}
	var result ExtractResult
	if json.Unmarshal(raw, &result) != nil {
		if err == nil {
			err = &APIError{Code: "python_executor_invalid_json", Message: "Python executor returned invalid JSON", Status: 502}
		}
		return nil, &APIError{Code: "python_executor_error", Message: err.Error(), Status: 502}
	}
	if result.OK {
		result.ProxyScheme = proxyScheme(proxyURL)
		return &result, nil
	}
	apiErr := &APIError{Code: result.Code, Message: result.Message, Status: 502}
	if result.Code == "" {
		apiErr.Code = "python_executor_failed"
	}
	if apiErr.Message == "" {
		apiErr.Message = "Python executor failed"
	}
	return &result, apiErr
}

type extractOutcome struct {
	result *ExtractResult
	err    error
}

type checkoutOutcome struct {
	hosted string
	pk     string
	err    error
}

type raceOutcome struct {
	result    *ExtractResult
	err       error
	candidate string
	attempt   int
}

type browserDeviceIDKey struct{}

func contextWithBrowserSession(ctx context.Context) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	if deviceIDFromContext(ctx) != "" {
		return ctx
	}
	return context.WithValue(ctx, browserDeviceIDKey{}, randomUUIDLike())
}

func deviceIDFromContext(ctx context.Context) string {
	if ctx == nil {
		return ""
	}
	if deviceID, _ := ctx.Value(browserDeviceIDKey{}).(string); strings.TrimSpace(deviceID) != "" {
		return strings.TrimSpace(deviceID)
	}
	return ""
}

func browserDeviceID(ctx context.Context) string {
	if deviceID := deviceIDFromContext(ctx); deviceID != "" {
		return deviceID
	}
	return randomUUIDLike()
}

func detachedAttemptContext(parent context.Context, timeout time.Duration) (context.Context, context.CancelFunc) {
	ctx := context.Background()
	if deviceID := deviceIDFromContext(parent); deviceID != "" {
		ctx = context.WithValue(ctx, browserDeviceIDKey{}, deviceID)
	}
	return context.WithTimeout(ctx, timeout)
}

func providerProxyPool(checkoutProxy string, proxyPool []string, rotations int) []string {
	base := []string{checkoutProxy}
	base = append(base, proxyPool...)
	out := make([]string, 0, len(base)*4)
	for _, proxyURL := range uniqueStrings(base...) {
		out = append(out, ProviderProxyCandidates(proxyURL, rotations)...)
	}
	return uniqueStrings(out...)
}

func (e *Extractor) createCheckoutReliable(ctx context.Context, token, proxyURL string) (string, string, error) {
	return e.createCheckoutSingle(ctx, token, proxyURL)
}

func (e *Extractor) createCheckoutSingle(ctx context.Context, token, proxyURL string) (string, string, error) {
	attemptCtx, cancel := context.WithTimeout(ctx, e.Config.Timeout)
	defer cancel()
	hosted, pk, err := e.createCheckoutTLS(attemptCtx, token, proxyURL)
	if err != nil && isCheckoutTransportRetryable(err) {
		hosted, pk, err = e.createCheckoutStd(attemptCtx, token, proxyURL)
	}
	return hosted, pk, err
}

func (e *Extractor) createCheckoutTLS(ctx context.Context, token, proxyURL string) (string, string, error) {
	client, err := e.newHTTPClient(proxyURL, e.Config.Timeout)
	if err != nil {
		return "", "", err
	}
	defer client.CloseIdleConnections()
	return e.createCheckout(ctx, client, token)
}

func (e *Extractor) probeProxyExitIP(ctx context.Context, proxyURL string) (string, error) {
	geo, err := e.probeProxyGeo(ctx, proxyURL)
	if err != nil {
		return "", err
	}
	return geo.IP, nil
}

func (e *Extractor) probeProxyGeo(ctx context.Context, proxyURL string) (*ProxyGeo, error) {
	if e.probeGeoHook != nil {
		return e.probeGeoHook(ctx, proxyURL)
	}
	if e.probeExitIPHook != nil {
		ip, err := e.probeExitIPHook(ctx, proxyURL)
		if err != nil {
			return nil, err
		}
		return &ProxyGeo{IP: ip}, nil
	}
	client, err := e.newStdHTTP1Client(proxyURL, 2500*time.Millisecond)
	if err != nil {
		return nil, err
	}
	var last error
	for _, endpoint := range []string{"http://ip-api.com/json/?fields=status,message,query,country,regionName,city,isp,org", "http://ipinfo.io/json"} {
		req, err := stdhttp.NewRequestWithContext(ctx, stdhttp.MethodGet, endpoint, nil)
		if err != nil {
			return nil, err
		}
		resp, err := client.Do(req)
		if err != nil {
			last = &APIError{Code: "proxy_ip_probe_network", Message: err.Error(), Status: 502}
			continue
		}
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 8192))
		resp.Body.Close()
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			if geo := parseProxyGeo(raw); geo != nil && geo.IP != "" {
				return geo, nil
			}
		}
		last = &APIError{Code: "proxy_ip_probe_failed", Message: fmt.Sprintf("proxy ip probe failed: HTTP %d", resp.StatusCode), Status: 502}
	}
	for _, endpoint := range []string{"http://api.ipify.org", "http://api64.ipify.org", "http://ifconfig.me/ip", "http://ifconfig.co/ip", "http://icanhazip.com", "http://ident.me"} {
		req, err := stdhttp.NewRequestWithContext(ctx, stdhttp.MethodGet, endpoint, nil)
		if err != nil {
			return nil, err
		}
		resp, err := client.Do(req)
		if err != nil {
			last = &APIError{Code: "proxy_ip_probe_network", Message: err.Error(), Status: 502}
			continue
		}
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 128))
		resp.Body.Close()
		ip := strings.TrimSpace(string(raw))
		if resp.StatusCode >= 200 && resp.StatusCode < 300 && ip != "" {
			return &ProxyGeo{IP: ip}, nil
		}
		last = &APIError{Code: "proxy_ip_probe_failed", Message: fmt.Sprintf("proxy ip probe failed: HTTP %d", resp.StatusCode), Status: 502}
	}
	if last != nil {
		return nil, last
	}
	return nil, &APIError{Code: "proxy_ip_probe_failed", Message: "proxy ip probe failed", Status: 502}
}

func parseProxyGeo(raw []byte) *ProxyGeo {
	var data map[string]any
	if json.Unmarshal(raw, &data) != nil {
		return nil
	}
	ip := stringValue(firstNonEmpty(data["query"], data["ip"]))
	if ip == "" {
		return nil
	}
	geo := &ProxyGeo{
		IP:      ip,
		Country: stringValue(data["country"]),
		Region:  stringValue(firstNonEmpty(data["regionName"], data["region"])),
		City:    stringValue(data["city"]),
		Org:     stringValue(firstNonEmpty(data["org"], data["isp"])),
	}
	if strings.EqualFold(stringValue(data["status"]), "fail") {
		return nil
	}
	return geo
}

func (e *Extractor) createCheckout(ctx context.Context, client httpDoer, token string) (string, string, error) {
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
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(e.Config.ChatGPTBase, "/")+"/backend-api/payments/checkout", bytes.NewReader(body))
	if err != nil {
		return "", "", err
	}
	setBrowserHeaders(req)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", "https://chatgpt.com")
	req.Header.Set("Referer", "https://chatgpt.com/")
	req.Header.Set("X-OpenAI-Target-Path", "/backend-api/payments/checkout")
	req.Header.Set("X-OpenAI-Target-Route", "/backend-api/payments/checkout")
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

type headerGetter interface {
	Get(string) string
}

func classifyNetworkError(code string, err error) *APIError {
	if err == nil {
		return &APIError{Code: code, Message: "network error", Status: 502}
	}
	if isProxyDNSError(err) {
		return &APIError{Code: "proxy_dns_error", Message: "代理域名无法解析", Status: 502}
	}
	text := err.Error()
	low := strings.ToLower(text)
	if strings.Contains(low, "proxy responded with non 200 code") ||
		strings.Contains(low, "connect tunnel failed") ||
		strings.Contains(low, "bad response from proxy") {
		return &APIError{Code: "proxy_connect_rejected", Message: text, Status: 502}
	}
	return &APIError{Code: code, Message: text, Status: 502}
}

func checkoutFailure(status int, header headerGetter, raw []byte, hosted string) *APIError {
	message := chatGPTErrorMessage(header, raw)
	switch status {
	case 401:
		code := "checkout_unauthorized"
		if strings.EqualFold(header.Get("x-openai-ide-error-code"), "token_invalidated") ||
			strings.Contains(strings.ToLower(message), "token_invalidated") ||
			strings.Contains(strings.ToLower(message), "invalidated") {
			code = "checkout_token_invalidated"
		}
		return &APIError{Code: code, Message: fmt.Sprintf("ChatGPT checkout rejected: HTTP 401 %s", message), Status: 401}
	case 403:
		return &APIError{Code: "checkout_forbidden", Message: fmt.Sprintf("ChatGPT checkout rejected: HTTP 403 %s", message), Status: 403}
	case 429:
		return &APIError{Code: "checkout_rate_limited", Message: fmt.Sprintf("ChatGPT checkout rate limited: HTTP 429 %s", message), Status: 429}
	}
	if status >= 200 && status < 300 && hosted == "" {
		return &APIError{Code: "checkout_no_hosted_url", Message: "ChatGPT checkout 响应未包含 hosted checkout URL " + chatGPTBodySummary(raw), Status: 502}
	}
	if message != "" {
		return &APIError{Code: "checkout_failed", Message: fmt.Sprintf("ChatGPT checkout failed: HTTP %d %s", status, message), Status: 502}
	}
	return &APIError{Code: "checkout_failed", Message: fmt.Sprintf("ChatGPT checkout failed: HTTP %d", status), Status: 502}
}

func chatGPTErrorMessage(header headerGetter, raw []byte) string {
	if encoded := header.Get("x-error-json"); encoded != "" {
		if decoded, err := base64.StdEncoding.DecodeString(encoded); err == nil {
			if msg := jsonErrorMessage(decoded); msg != "" {
				return sanitizeErrorSnippet(msg, 240)
			}
			return sanitizeErrorSnippet(string(decoded), 240)
		}
	}
	if msg := jsonErrorMessage(raw); msg != "" {
		return sanitizeErrorSnippet(msg, 240)
	}
	text := strings.TrimSpace(string(raw))
	return sanitizeErrorSnippet(text, 240)
}

func jsonErrorMessage(raw []byte) string {
	var data map[string]any
	if json.Unmarshal(raw, &data) != nil {
		return ""
	}
	errObj, _ := data["error"].(map[string]any)
	if errObj == nil {
		return ""
	}
	parts := []string{}
	for _, key := range []string{"code", "type", "message"} {
		if value := stringValue(errObj[key]); value != "" {
			parts = append(parts, value)
		}
	}
	return strings.Join(parts, " ")
}

func chatGPTBodySummary(raw []byte) string {
	var data map[string]any
	if json.Unmarshal(raw, &data) == nil {
		keys := make([]string, 0, len(data))
		for key := range data {
			keys = append(keys, key)
		}
		if len(keys) > 0 {
			return "keys=" + strings.Join(keys, ",")
		}
	}
	text := strings.TrimSpace(string(raw))
	if text == "" {
		return "body=empty"
	}
	return "body=" + sanitizeErrorSnippet(text, 160)
}

func (e *Extractor) fetchCheckoutInit(ctx context.Context, client httpDoer, hosted, checkoutPK string) (string, string, map[string]any, error) {
	m := csRe.FindStringSubmatch(hosted)
	if len(m) < 2 {
		return "", "", nil, &APIError{Code: "checkout_session_missing", Message: "hosted checkout URL 缺少 Stripe session id", Status: 502}
	}
	cs := m[1]
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, hosted, nil)
	if err != nil {
		return "", "", nil, err
	}
	setBrowserHeaders(req)
	resp, err := client.Do(req)
	if err != nil {
		apiErr := classifyNetworkError("stripe_checkout_network_error", err)
		return "", "", nil, apiErr
	}
	defer resp.Body.Close()
	html, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", "", nil, &APIError{Code: "stripe_checkout_failed", Message: fmt.Sprintf("Stripe hosted page failed: HTTP %d", resp.StatusCode), Status: 502}
	}
	pkCandidates := uniqueStrings(
		selectPublishableKey(string(html), strings.HasPrefix(cs, "cs_live_")),
		checkoutPK,
	)
	for i := 0; i < len(pkCandidates); {
		if !publishableMatchesSession(pkCandidates[i], cs) {
			pkCandidates = append(pkCandidates[:i], pkCandidates[i+1:]...)
			continue
		}
		i++
	}
	if len(pkCandidates) == 0 {
		return "", "", nil, &APIError{Code: "stripe_publishable_key_missing", Message: "Stripe 页面未解析到 publishable key", Status: 502}
	}

	var lastErr error
	referers := uniqueStrings(hosted, "https://pay.openai.com/")
	for _, pk := range pkCandidates {
		for _, referer := range referers {
			init, err := e.postHostedCheckoutInit(ctx, client, cs, pk, referer)
			if err == nil {
				normalizeCheckoutInitURLs(init, hosted)
				return pk, cs, init, nil
			}
			lastErr = err
			if !isRetryable(err) && codeOf(err) != "stripe_init_failed" {
				break
			}
		}
	}
	for _, pk := range pkCandidates {
		for _, referer := range referers {
			init, err := e.postCustomCheckoutInit(ctx, client, cs, pk, referer)
			if err == nil {
				normalizeCheckoutInitURLs(init, hosted)
				return pk, cs, init, nil
			}
			lastErr = err
			if !isRetryable(err) && codeOf(err) != "stripe_init_failed" {
				break
			}
		}
	}
	if lastErr != nil {
		return "", "", nil, lastErr
	}
	return "", "", nil, &APIError{Code: "stripe_init_failed", Message: "Stripe init failed", Status: 502}
}

func (e *Extractor) postHostedCheckoutInit(ctx context.Context, client httpDoer, cs, pk, referer string) (map[string]any, error) {
	form := url.Values{
		"key":              {pk},
		"eid":              {"NA"},
		"browser_locale":   {"en-US"},
		"browser_timezone": {"Asia/Shanghai"},
		"redirect_type":    {"url"},
	}
	return e.postCheckoutInit(ctx, client, cs, form, referer)
}

func (e *Extractor) postCustomCheckoutInit(ctx context.Context, client httpDoer, cs, pk, referer string) (map[string]any, error) {
	form := url.Values{
		"browser_locale":                                                  {"en-US"},
		"browser_timezone":                                                {"Asia/Shanghai"},
		"elements_session_client[client_betas][0]":                        {"custom_checkout_server_updates_1"},
		"elements_session_client[client_betas][1]":                        {"custom_checkout_manual_approval_1"},
		"elements_session_client[elements_init_source]":                   {"custom_checkout"},
		"elements_session_client[referrer_host]":                          {"chatgpt.com"},
		"elements_session_client[stripe_js_id]":                           {randomUUIDLike()},
		"elements_session_client[locale]":                                 {"en"},
		"elements_session_client[is_aggregation_expected]":                {"false"},
		"elements_options_client[saved_payment_method][enable_save]":      {"never"},
		"elements_options_client[saved_payment_method][enable_redisplay]": {"never"},
		"key":             {pk},
		"_stripe_version": {stripeVersionFull},
	}
	return e.postCheckoutInit(ctx, client, cs, form, referer)
}

func (e *Extractor) postCheckoutInit(ctx context.Context, client httpDoer, cs string, form url.Values, referer string) (map[string]any, error) {
	initURL := strings.TrimRight(e.Config.StripeBase, "/") + "/v1/payment_pages/" + cs + "/init"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, initURL, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, err
	}
	setStripeAPIHeaders(req)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	if referer != "" {
		req.Header.Set("Origin", originForReferer(referer))
		req.Header.Set("Referer", referer)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, classifyNetworkError("stripe_init_network_error", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	var init map[string]any
	if err := json.Unmarshal(raw, &init); err != nil {
		return nil, &APIError{Code: "stripe_init_invalid_json", Message: "Stripe init 响应不是 JSON", Status: 502}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, &APIError{Code: "stripe_init_failed", Message: fmt.Sprintf("Stripe init failed: HTTP %d %s", resp.StatusCode, stripeErrorMessage(raw)), Status: 502}
	}
	return init, nil
}

func normalizeCheckoutInitURLs(init map[string]any, fallback string) {
	if init == nil {
		return
	}
	hosted := effectiveHostedCheckoutURL(init, fallback)
	if hosted != "" {
		init["hosted_checkout_url"] = hosted
	}
	if stringValue(init["url"]) == "" && stringValue(init["stripe_hosted_url"]) == "" && fallback != "" {
		init["url"] = fallback
	}
}

func effectiveHostedCheckoutURL(init map[string]any, fallback string) string {
	if init != nil {
		for _, raw := range []any{init["stripe_hosted_url"], init["hosted_checkout_url"], init["url"]} {
			if value := stringValue(raw); strings.Contains(value, "/c/pay/") {
				return value
			}
		}
	}
	if strings.Contains(fallback, "/c/pay/") {
		return fallback
	}
	return ""
}

func publishableMatchesSession(pk, cs string) bool {
	return (strings.HasPrefix(cs, "cs_live_") && strings.HasPrefix(pk, "pk_live_")) ||
		(strings.HasPrefix(cs, "cs_test_") && strings.HasPrefix(pk, "pk_test_"))
}

func uniqueStrings(values ...string) []string {
	seen := make(map[string]struct{}, len(values))
	out := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		if _, ok := seen[value]; ok {
			continue
		}
		seen[value] = struct{}{}
		out = append(out, value)
	}
	return out
}

func selectPublishableKey(html string, live bool) string {
	keys := pkRe.FindAllString(html, -1)
	preferred := "pk_test_"
	if live {
		preferred = "pk_live_"
	}
	for _, key := range keys {
		if strings.HasPrefix(key, preferred) {
			return key
		}
	}
	if len(keys) > 0 {
		return keys[0]
	}
	return ""
}

type zeroGate struct {
	AmountDue *int64
	Currency  string
}

func (e *Extractor) verifyAmount(init map[string]any) (*zeroGate, error) {
	invoice, _ := init["invoice"].(map[string]any)
	if invoice == nil {
		return &zeroGate{}, &APIError{Code: "checkout_guard_failed", Message: "无法确认 Stripe 发票应付金额，已取消提链", Status: 422}
	}
	amount, ok := payableCheckoutAmount(init)
	currency := strings.ToLower(stringValue(firstNonEmpty(init["currency"], invoice["currency"])))
	if !ok {
		return &zeroGate{Currency: currency}, &APIError{Code: "checkout_guard_failed", Message: "Stripe 发票金额格式异常，已取消提链", Status: 422}
	}
	methods, _ := init["payment_method_types"].([]any)
	hasPayPal := false
	for _, item := range methods {
		if strings.EqualFold(stringValue(item), "paypal") {
			hasPayPal = true
			break
		}
	}
	if !hasPayPal {
		hasPayPal = containsString(init, "paypal")
	}
	if !e.Config.AllowNonZero && amount != 0 {
		return &zeroGate{AmountDue: &amount, Currency: currency}, &APIError{Code: "non_zero_amount", Message: "实检金额非 0，本次不输出通道", Status: 409}
	}
	return &zeroGate{AmountDue: &amount, Currency: currency}, nil
}

func payableCheckoutAmount(init map[string]any) (int64, bool) {
	totalSummary, _ := init["total_summary"].(map[string]any)
	if amount, ok := int64Value(totalSummary["due"]); ok {
		return amount, true
	}
	invoice, _ := init["invoice"].(map[string]any)
	if invoice != nil && stringValue(invoice["billing_cycle_anchor"]) != "" && !boolValue(invoice["has_prorations"]) {
		return 0, true
	}
	return int64Value(invoice["amount_due"])
}

func payableCheckoutAmountString(init map[string]any) string {
	if amount, ok := payableCheckoutAmount(init); ok {
		return fmt.Sprint(amount)
	}
	return "0"
}

func containsString(value any, needle string) bool {
	switch v := value.(type) {
	case string:
		return strings.Contains(strings.ToLower(v), strings.ToLower(needle))
	case map[string]any:
		for key, child := range v {
			if strings.Contains(strings.ToLower(key), strings.ToLower(needle)) || containsString(child, needle) {
				return true
			}
		}
	case []any:
		for _, child := range v {
			if containsString(child, needle) {
				return true
			}
		}
	}
	return false
}

func (e *Extractor) prepareStripePayPal(ctx context.Context, client httpDoer, pk, cs, hosted string, init map[string]any) (map[string]any, string) {
	latest := init
	amount, _ := int64Value(firstNonEmpty(mapValueAny(latest, "invoice", "amount_due"), 0))
	currency := strings.ToLower(stringValue(firstNonEmpty(latest["currency"], mapValueAny(latest, "invoice", "currency"), "eur")))
	taxCountries := uniqueStrings(strings.ToUpper(e.Config.Country), "US")
	if _, err := e.stripeForm(ctx, client, http.MethodGet, strings.TrimRight(e.Config.StripeBase, "/")+"/v1/payment_pages/allowed_origins", url.Values{"key": {pk}, "session_id": {cs}}, "https://js.stripe.com/"); err != nil {
		log.Printf("stripe prepare step failed step=allowed_origins code=%s msg=%q", codeOf(err), sanitizeErrorSnippet(err.Error(), 180))
	}
	elementsForm := url.Values{
		"client_betas[0]":                          {"google_pay_beta_1"},
		"client_betas[1]":                          {"disable_deferred_intent_client_validation_beta_1"},
		"client_betas[2]":                          {"blocked_card_brands_beta_2"},
		"deferred_intent[mode]":                    {"subscription"},
		"deferred_intent[amount]":                  {fmt.Sprint(amount)},
		"deferred_intent[currency]":                {currency},
		"deferred_intent[setup_future_usage]":      {"off_session"},
		"deferred_intent[payment_method_types][0]": {"card"},
		"currency":                                 {currency},
		"key":                                      {pk},
		"elements_init_source":                     {"checkout"},
		"hosted_surface":                           {"checkout"},
		"referrer_host":                            {"pay.openai.com"},
		"stripe_js_id":                             {randomUUIDLike()},
		"locale":                                   {"en-US"},
		"type":                                     {"deferred_intent"},
		"checkout_session_id":                      {cs},
	}
	payload, err := e.stripeForm(ctx, client, http.MethodGet, strings.TrimRight(e.Config.StripeBase, "/")+"/v1/elements/sessions", elementsForm, "https://js.stripe.com/")
	if err != nil {
		log.Printf("stripe prepare step failed step=elements code=%s msg=%q", codeOf(err), sanitizeErrorSnippet(err.Error(), 240))
	} else if len(payload) > 0 {
		if payload["total_summary"] != nil || payload["invoice"] != nil || payload["init_checksum"] != nil {
			latest = mergeMaps(latest, payload)
		}
		if pmURL := existingPayPalAuthorizeURL(payload); pmURL != "" {
			return latest, pmURL
		}
	}

	for _, taxCountry := range taxCountries {
		payload, err := e.stripeForm(ctx, client, http.MethodPost, strings.TrimRight(e.Config.StripeBase, "/")+"/v1/payment_pages/"+cs, url.Values{"eid": {"NA"}, "tax_region[country]": {taxCountry}, "key": {pk}}, "https://pay.openai.com/")
		if err != nil {
			log.Printf("stripe prepare step failed step=tax_region_%s code=%s msg=%q", taxCountry, codeOf(err), sanitizeErrorSnippet(err.Error(), 240))
			continue
		}
		if len(payload) > 0 {
			if payload["total_summary"] != nil || payload["invoice"] != nil || payload["init_checksum"] != nil {
				latest = mergeMaps(latest, payload)
			}
			if pmURL := existingPayPalAuthorizeURL(payload); pmURL != "" {
				return latest, pmURL
			}
		}
		break
	}
	return latest, ""
}

func (e *Extractor) stripeForm(ctx context.Context, client httpDoer, method, endpoint string, form url.Values, referer string) (map[string]any, error) {
	var body io.Reader
	if method == http.MethodGet {
		endpoint += "?" + form.Encode()
	} else {
		body = strings.NewReader(form.Encode())
	}
	req, err := http.NewRequestWithContext(ctx, method, endpoint, body)
	if err != nil {
		return nil, err
	}
	setBrowserHeaders(req)
	if method != http.MethodGet {
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Origin", "https://pay.openai.com")
	req.Header.Set("Referer", strings.Split(referer, "#")[0])
	resp, err := client.Do(req)
	if err != nil {
		return nil, classifyNetworkError("stripe_prepare_network_error", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	var data map[string]any
	if err := json.Unmarshal(raw, &data); err != nil {
		return nil, &APIError{Code: "stripe_prepare_invalid_json", Message: "Stripe prepare 响应不是 JSON", Status: 502}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return data, &APIError{Code: "stripe_prepare_failed", Message: fmt.Sprintf("Stripe prepare failed: HTTP %d %s", resp.StatusCode, stripeErrorMessage(raw)), Status: 502}
	}
	return data, nil
}

func mergeMaps(base, overlay map[string]any) map[string]any {
	out := make(map[string]any, len(base)+len(overlay))
	for key, value := range base {
		out[key] = value
	}
	for key, value := range overlay {
		out[key] = value
	}
	return out
}

func randomHex(n int) string {
	if n <= 0 {
		return ""
	}
	buf := make([]byte, n)
	if _, err := rand.Read(buf); err != nil {
		return time.Now().UTC().Format("20060102150405")
	}
	const alphabet = "0123456789abcdef"
	out := make([]byte, n*2)
	for i, b := range buf {
		out[i*2] = alphabet[b>>4]
		out[i*2+1] = alphabet[b&0x0f]
	}
	return string(out)
}

func randomAlphaNum(n int) string {
	if n <= 0 {
		return ""
	}
	buf := make([]byte, n)
	if _, err := rand.Read(buf); err != nil {
		return time.Now().UTC().Format("20060102150405")
	}
	const alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
	out := make([]byte, n)
	for i, b := range buf {
		out[i] = alphabet[int(b)%len(alphabet)]
	}
	return string(out)
}

func randomUUIDLike() string {
	h := randomHex(16)
	if len(h) < 32 {
		h = fmt.Sprintf("%032s", h)
	}
	return h[0:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:32]
}

func (e *Extractor) confirmPayPal(ctx context.Context, client httpDoer, pk, cs string, init map[string]any) (string, error) {
	if err := e.preConfirmPayPal(ctx, client, pk, cs, init); err != nil {
		log.Printf("stripe pre_confirm failed code=%s msg=%q", codeOf(err), sanitizeErrorSnippet(err.Error(), 180))
	}
	amounts := displayAmounts(init)
	amountDue, _ := int64Value(firstNonEmpty(mapValueAny(init, "invoice", "amount_due"), 0))
	returnURL := rawConfirmReturnURLForInit(init, cs)
	referrerURL := checkoutReferrerURL(init, cs)
	var lastErr error
	for _, country := range confirmCountries(e.Config.Country, init) {
		form := e.payPalConfirmForm(pk, cs, init, amounts, amountDue, returnURL, referrerURL, country)
		pm, err := e.postConfirmPayPal(ctx, client, cs, form)
		if err == nil {
			return pm, nil
		}
		lastErr = err
		if strings.Contains(returnURL, "#") {
			form.Set("return_url", strings.Split(returnURL, "#")[0])
			pm, err = e.postConfirmPayPal(ctx, client, cs, form)
			if err == nil {
				return pm, nil
			}
			lastErr = err
		}
	}
	return "", lastErr
}

func (e *Extractor) confirmPayPalWithPaymentMethod(ctx context.Context, client httpDoer, pk, cs string, init map[string]any) (string, error) {
	if err := e.preConfirmPayPal(ctx, client, pk, cs, init); err != nil {
		log.Printf("stripe pre_confirm failed code=%s msg=%q", codeOf(err), sanitizeErrorSnippet(err.Error(), 180))
	}
	amounts := displayAmounts(init)
	amountDue, _ := int64Value(firstNonEmpty(mapValueAny(init, "invoice", "amount_due"), 0))
	returnURL := rawConfirmReturnURLForInit(init, cs)
	referrerURL := checkoutReferrerURL(init, cs)
	var lastErr error
	for _, country := range confirmCountries(e.Config.Country, init) {
		pmID, err := e.createPayPalPaymentMethod(ctx, client, pk, cs, init, country)
		if err != nil {
			lastErr = err
			continue
		}
		form := e.payPalConfirmWithPMForm(pk, cs, init, amounts, amountDue, returnURL, referrerURL, pmID)
		pm, err := e.postConfirmPayPal(ctx, client, cs, form)
		if err == nil {
			return pm, nil
		}
		lastErr = err
		if strings.Contains(returnURL, "#") {
			form.Set("return_url", strings.Split(returnURL, "#")[0])
			pm, err = e.postConfirmPayPal(ctx, client, cs, form)
			if err == nil {
				return pm, nil
			}
			lastErr = err
		}
	}
	return "", lastErr
}

type stripeCustomContext struct {
	StripeJSID              string
	ElementsSessionID       string
	ElementsSessionConfigID string
	ConfigID                string
	InitChecksum            string
	CheckoutAmount          string
	Locale                  string
}

func newStripeCustomContext(cs string, init map[string]any) stripeCustomContext {
	configID := stringValue(init["config_id"])
	if configID == "" {
		configID = randomUUIDLike()
	}
	amount := payableCheckoutAmountString(init)
	return stripeCustomContext{
		StripeJSID:              randomUUIDLike(),
		ElementsSessionID:       "elements_session_" + randomHex(6),
		ElementsSessionConfigID: configID,
		ConfigID:                configID,
		InitChecksum:            stringValue(init["init_checksum"]),
		CheckoutAmount:          amount,
		Locale:                  "en",
	}
}

func (e *Extractor) confirmPayPalCustom(ctx context.Context, chatGPTClient httpDoer, token, proxyURL string, stripeClient httpDoer, pk, cs string, init map[string]any) (string, error) {
	if !strings.EqualFold(stringValue(init["ui_mode"]), "custom") {
		return "", &APIError{Code: "stripe_custom_not_needed", Message: "not custom checkout", Status: 502}
	}
	customCtx := newStripeCustomContext(cs, init)
	var lastErr error
	for _, country := range confirmCountries("JP", init) {
		pmID, err := e.createCustomPayPalPaymentMethod(ctx, stripeClient, pk, cs, init, customCtx, country)
		if err != nil {
			lastErr = err
			continue
		}
		pmURL, err := e.confirmCustomPayPalWithPaymentMethod(ctx, chatGPTClient, token, proxyURL, stripeClient, pk, cs, init, customCtx, pmID)
		if err == nil && pmURL != "" {
			return pmURL, nil
		}
		lastErr = err
	}
	if lastErr != nil {
		return "", lastErr
	}
	return "", &APIError{Code: "stripe_confirm_failed", Message: "Stripe custom confirm did not return PayPal authorize URL", Status: 502}
}

func (e *Extractor) confirmPayPalCustomAcrossProxies(ctx context.Context, chatGPTClient httpDoer, token string, providerProxies []string, pk, cs string, init map[string]any) (string, string, error) {
	if !strings.EqualFold(stringValue(init["ui_mode"]), "custom") {
		return "", "", &APIError{Code: "stripe_custom_not_needed", Message: "not custom checkout", Status: 502}
	}
	if len(providerProxies) == 0 {
		providerProxies = []string{""}
	}
	parallel := e.Config.RaceParallel
	if parallel <= 0 {
		parallel = 1
	}
	if parallel > len(providerProxies) {
		parallel = len(providerProxies)
	}
	if parallel < 1 {
		parallel = 1
	}

	type outcome struct {
		url   string
		proxy string
		err   error
	}
	sem := make(chan struct{}, parallel)
	out := make(chan outcome, len(providerProxies))
	var wg sync.WaitGroup
	for _, proxyURL := range providerProxies {
		proxyURL := proxyURL
		wg.Add(1)
		go func() {
			defer wg.Done()
			select {
			case sem <- struct{}{}:
				defer func() { <-sem }()
			case <-ctx.Done():
				out <- outcome{proxy: proxyURL, err: &APIError{Code: "client_timeout", Message: ctx.Err().Error(), Status: 504}}
				return
			}
			attemptCtx, cancel := detachedAttemptContext(ctx, e.raceAttemptTimeout())
			defer cancel()
			client, err := e.newHTTPClient(proxyURL, e.Config.Timeout)
			if err != nil {
				out <- outcome{proxy: proxyURL, err: err}
				return
			}
			defer client.CloseIdleConnections()
			approver := chatGPTClient
			if approver == nil {
				approver = client
			} else if proxyURL != "" && proxyURL != e.proxyOf(approver) {
				if tlsClient, ok := approver.(tlsHTTPDoer); ok {
					if cloned, err := e.newHTTPClientWithJar(proxyURL, e.Config.Timeout, tlsClient.GetCookieJar()); err == nil {
						defer cloned.CloseIdleConnections()
						approver = cloned
					}
				}
			}
			pmURL, err := e.confirmPayPalCustom(attemptCtx, approver, token, proxyURL, client, pk, cs, init)
			out <- outcome{url: pmURL, proxy: proxyURL, err: err}
		}()
	}
	go func() {
		wg.Wait()
		close(out)
	}()

	var lastErr error
	for item := range out {
		if item.err == nil && item.url != "" {
			go func() {
				for range out {
				}
			}()
			return item.url, item.proxy, nil
		}
		if item.err != nil {
			lastErr = preferRaceError(lastErr, item.err)
		}
	}
	if lastErr != nil {
		return "", "", lastErr
	}
	return "", "", &APIError{Code: "stripe_confirm_failed", Message: "Stripe custom confirm did not return PayPal authorize URL", Status: 502}
}

func (e *Extractor) proxyOf(client httpDoer) string {
	if tlsClient, ok := client.(tlsHTTPDoer); ok {
		return tlsClient.GetProxy()
	}
	return ""
}

func (e *Extractor) attachProxyGeo(ctx context.Context, result *ExtractResult, proxyURL string) {
	if result == nil || strings.TrimSpace(proxyURL) == "" {
		return
	}
	probeCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	geo, err := e.probeProxyGeo(probeCtx, proxyURL)
	if err != nil || geo == nil {
		return
	}
	result.ProxyIP = geo.IP
	result.ProxyCountry = geo.Country
	result.ProxyRegion = geo.Region
	result.ProxyCity = geo.City
	result.ProxyOrg = geo.Org
}

func (e *Extractor) createCustomPayPalPaymentMethod(ctx context.Context, client httpDoer, pk, cs string, init map[string]any, customCtx stripeCustomContext, country string) (string, error) {
	addr := billingAddress(country)
	email := stringValue(init["customer_email"])
	if email == "" {
		email = "buyer@example.com"
	}
	form := url.Values{
		"billing_details[name]":                                                    {"Taro Yamada"},
		"billing_details[email]":                                                   {email},
		"billing_details[address][country]":                                        {addr.Country},
		"billing_details[address][line1]":                                          {addr.Line1},
		"billing_details[address][city]":                                           {addr.City},
		"billing_details[address][postal_code]":                                    {addr.PostalCode},
		"billing_details[address][state]":                                          {addr.State},
		"type":                                                                     {"paypal"},
		"payment_user_agent":                                                       {stripeCustomPaymentUserAgent()},
		"referrer":                                                                 {"https://chatgpt.com"},
		"time_on_page":                                                             {"31000"},
		"client_attribution_metadata[checkout_session_id]":                         {cs},
		"client_attribution_metadata[client_session_id]":                           {customCtx.StripeJSID},
		"client_attribution_metadata[checkout_config_id]":                          {customCtx.ConfigID},
		"client_attribution_metadata[elements_session_id]":                         {customCtx.ElementsSessionID},
		"client_attribution_metadata[elements_session_config_id]":                  {customCtx.ElementsSessionConfigID},
		"client_attribution_metadata[merchant_integration_source]":                 {"elements"},
		"client_attribution_metadata[merchant_integration_subtype]":                {"payment-element"},
		"client_attribution_metadata[merchant_integration_version]":                {"2021"},
		"client_attribution_metadata[payment_intent_creation_flow]":                {"deferred"},
		"client_attribution_metadata[payment_method_selection_flow]":               {"automatic"},
		"client_attribution_metadata[merchant_integration_additional_elements][0]": {"payment"},
		"client_attribution_metadata[merchant_integration_additional_elements][1]": {"address"},
		"key":             {pk},
		"_stripe_version": {stripeVersionFull},
	}
	endpoint := strings.TrimRight(e.Config.StripeBase, "/") + "/v1/payment_methods"
	payload, err := e.postStripeForm(ctx, client, endpoint, form, "")
	if err != nil {
		return "", err
	}
	pmID := stringValue(payload["id"])
	if !strings.HasPrefix(pmID, "pm_") {
		return "", &APIError{Code: "stripe_payment_method_failed", Message: "Stripe payment_method missing id", Status: 502}
	}
	return pmID, nil
}

func (e *Extractor) confirmCustomPayPalWithPaymentMethod(ctx context.Context, chatGPTClient httpDoer, token, proxyURL string, stripeClient httpDoer, pk, cs string, init map[string]any, customCtx stripeCustomContext, pmID string) (string, error) {
	form := url.Values{
		"guid":                                   {randomHex(16)},
		"muid":                                   {randomHex(16)},
		"sid":                                    {randomHex(16)},
		"payment_method":                         {pmID},
		"init_checksum":                          {customCtx.InitChecksum},
		"version":                                {stripeRuntimeVersion},
		"expected_amount":                        {customCtx.CheckoutAmount},
		"expected_payment_method_type":           {"paypal"},
		"return_url":                             {customConfirmReturnURL(cs, init)},
		"elements_session_client[session_id]":    {customCtx.ElementsSessionID},
		"elements_session_client[locale]":        {customCtx.Locale},
		"elements_session_client[referrer_host]": {"chatgpt.com"},
		"elements_session_client[is_aggregation_expected]":                         {"false"},
		"elements_session_client[elements_init_source]":                            {"custom_checkout"},
		"elements_session_client[stripe_js_id]":                                    {customCtx.StripeJSID},
		"elements_session_client[client_betas][0]":                                 {"custom_checkout_server_updates_1"},
		"elements_session_client[client_betas][1]":                                 {"custom_checkout_manual_approval_1"},
		"elements_options_client[saved_payment_method][enable_save]":               {"never"},
		"elements_options_client[saved_payment_method][enable_redisplay]":          {"never"},
		"client_attribution_metadata[client_session_id]":                           {customCtx.StripeJSID},
		"client_attribution_metadata[checkout_session_id]":                         {cs},
		"client_attribution_metadata[checkout_config_id]":                          {customCtx.ConfigID},
		"client_attribution_metadata[elements_session_id]":                         {customCtx.ElementsSessionID},
		"client_attribution_metadata[elements_session_config_id]":                  {customCtx.ElementsSessionConfigID},
		"client_attribution_metadata[merchant_integration_source]":                 {"checkout"},
		"client_attribution_metadata[merchant_integration_subtype]":                {"payment-element"},
		"client_attribution_metadata[merchant_integration_version]":                {"custom"},
		"client_attribution_metadata[payment_intent_creation_flow]":                {"deferred"},
		"client_attribution_metadata[payment_method_selection_flow]":               {"automatic"},
		"client_attribution_metadata[merchant_integration_additional_elements][0]": {"payment"},
		"client_attribution_metadata[merchant_integration_additional_elements][1]": {"address"},
		"consent[terms_of_service]":                                                {"accepted"},
		"key":                                                                      {pk},
		"_stripe_version":                                                          {stripeVersionFull},
	}
	payload, err := e.postConfirmPayload(ctx, stripeClient, cs, form, "")
	if err != nil {
		return "", err
	}
	if pmURL := redirectURLFromPayload(payload); pmURL != "" {
		return e.resolveExternalRedirect(ctx, stripeClient, pmURL), nil
	}
	if submission, _ := payload["submission_attempt"].(map[string]any); strings.EqualFold(stringValue(submission["state"]), "requires_approval") {
		return e.approveAndPollStripeRedirect(ctx, chatGPTClient, token, proxyURL, stripeClient, pk, cs, init)
	}
	if pmURL, err := e.pollStripePaymentPageRedirect(ctx, stripeClient, cs, pk); err == nil && pmURL != "" {
		return pmURL, nil
	} else if err != nil {
		return "", err
	}
	return "", &APIError{Code: "stripe_confirm_failed", Message: "Stripe custom confirm did not return redirect", Status: 502}
}

func (e *Extractor) approveAndPollStripeRedirect(ctx context.Context, chatGPTClient httpDoer, token, proxyURL string, stripeClient httpDoer, pk, cs string, init map[string]any) (string, error) {
	approveTimeout := 12 * time.Second
	if e.Config.Timeout > 0 && e.Config.Timeout < approveTimeout {
		approveTimeout = e.Config.Timeout
	}
	approveCtx, cancelApprove := context.WithTimeout(ctx, approveTimeout)
	approveDone := make(chan error, 1)
	go func() {
		approveDone <- e.chatGPTApprove(approveCtx, chatGPTClient, token, proxyURL, cs, init)
	}()

	pmURL, pollErr := e.pollStripePaymentPageRedirect(ctx, stripeClient, cs, pk)
	cancelApprove()
	select {
	case approveErr := <-approveDone:
		if approveErr != nil {
			log.Printf("checkout approve did not complete before poll result code=%s msg=%q", codeOf(approveErr), sanitizeErrorSnippet(approveErr.Error(), 180))
		}
	default:
		log.Printf("checkout approve still running when poll completed; cancelled")
	}
	if pmURL != "" && pollErr == nil {
		return pmURL, nil
	}
	if pollErr != nil {
		return "", pollErr
	}
	return "", &APIError{Code: "stripe_confirm_failed", Message: "Stripe custom approval did not return redirect", Status: 502}
}

func stripeCustomPaymentUserAgent() string {
	return "stripe.js/" + stripeRuntimeVersion + "; stripe-js-v3/" + stripeRuntimeVersion + "; payment-element; deferred-intent"
}

func (e *Extractor) postStripeForm(ctx context.Context, client httpDoer, endpoint string, form url.Values, referer string) (map[string]any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, err
	}
	setStripeAPIHeaders(req)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	if referer != "" {
		req.Header.Set("Origin", originForReferer(referer))
		req.Header.Set("Referer", referer)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, classifyNetworkError("stripe_prepare_network_error", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	var payload map[string]any
	if json.Unmarshal(raw, &payload) != nil {
		return nil, &APIError{Code: "stripe_prepare_invalid_json", Message: "Stripe 响应不是 JSON", Status: 502}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return payload, &APIError{Code: "stripe_prepare_failed", Message: fmt.Sprintf("Stripe request failed: HTTP %d %s", resp.StatusCode, stripeErrorMessage(raw)), Status: 502}
	}
	return payload, nil
}

func (e *Extractor) postConfirmPayload(ctx context.Context, client httpDoer, cs string, form url.Values, referer string) (map[string]any, error) {
	endpoint := strings.TrimRight(e.Config.StripeBase, "/") + "/v1/payment_pages/" + cs + "/confirm"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, err
	}
	setStripeAPIHeaders(req)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	if referer != "" {
		req.Header.Set("Origin", originForReferer(referer))
		req.Header.Set("Referer", referer)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, classifyNetworkError("stripe_confirm_network_error", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	var payload map[string]any
	if json.Unmarshal(raw, &payload) != nil {
		return nil, &APIError{Code: "stripe_confirm_failed", Message: "Stripe confirm 响应不是 JSON", Status: 502}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		log.Printf("stripe custom confirm failed status=%d %s", resp.StatusCode, confirmDebugSummary(raw))
		return payload, &APIError{Code: "stripe_confirm_failed", Message: fmt.Sprintf("Stripe confirm failed: HTTP %d %s", resp.StatusCode, stripeErrorMessage(raw)), Status: 502}
	}
	return payload, nil
}

func originForReferer(referer string) string {
	u, err := url.Parse(referer)
	if err != nil || u.Scheme == "" || u.Host == "" {
		return "https://pay.openai.com"
	}
	return u.Scheme + "://" + u.Host
}

func redirectURLFromPayload(payload map[string]any) string {
	if payload == nil {
		return ""
	}
	if pmURL := existingPayPalAuthorizeURL(payload); pmURL != "" {
		return pmURL
	}
	if nextAction, _ := payload["next_action"].(map[string]any); strings.EqualFold(stringValue(nextAction["type"]), "redirect_to_url") {
		if redirect, _ := nextAction["redirect_to_url"].(map[string]any); redirect != nil {
			if raw := stringValue(redirect["url"]); raw != "" {
				return raw
			}
		}
	}
	for _, key := range []string{"setup_intent", "payment_intent"} {
		if nested, _ := payload[key].(map[string]any); nested != nil {
			if raw := redirectURLFromPayload(nested); raw != "" {
				return raw
			}
		}
	}
	return ""
}

func customConfirmReturnURL(cs string, init map[string]any) string {
	hosted := toOpenAIPayURL(stringValue(firstNonEmpty(init["stripe_hosted_url"], init["hosted_checkout_url"], init["url"])))
	if hosted == "" {
		hosted = "https://pay.openai.com/c/pay/" + cs
	}
	success := stringValue(firstNonEmpty(init["return_url"], init["success_url"]))
	if success == "" {
		success = "https://chatgpt.com/checkout/verify?stripe_session_id=" + url.QueryEscape(cs) + "&processor_entity=openai_llc&plan_type=plus"
	}
	return appendQueryParams(hosted, map[string]string{"success_return_url": success})
}

func toOpenAIPayURL(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	u, err := url.Parse(raw)
	if err != nil {
		return raw
	}
	if strings.EqualFold(u.Host, "checkout.stripe.com") {
		u.Host = "pay.openai.com"
		u.Scheme = "https"
		return u.String()
	}
	return raw
}

func (e *Extractor) chatGPTApprove(ctx context.Context, client httpDoer, token, proxyURL, cs string, init map[string]any) error {
	if client == nil {
		var err error
		client, err = e.newHTTPClient(proxyURL, e.Config.Timeout)
		if err != nil {
			return err
		}
		defer client.CloseIdleConnections()
	}
	processor := processorEntityForCountry(e.Config.Country, stringValue(init["processor_entity"]))
	if err := e.chatGPTPing(ctx, client, token); err != nil {
		log.Printf("checkout approve warmup failed code=%s msg=%q", codeOf(err), sanitizeErrorSnippet(err.Error(), 160))
	}
	payload, _ := json.Marshal(map[string]any{
		"checkout_session_id": cs,
		"processor_entity":    processor,
	})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(e.Config.ChatGPTBase, "/")+"/backend-api/payments/checkout/approve", bytes.NewReader(payload))
	if err != nil {
		return err
	}
	setBrowserHeaders(req)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", "https://chatgpt.com")
	req.Header.Set("Referer", "https://chatgpt.com/checkout/"+processor+"/"+cs)
	req.Header.Set("X-OpenAI-Target-Path", "/backend-api/payments/checkout/approve")
	req.Header.Set("X-OpenAI-Target-Route", "/backend-api/payments/checkout/approve")
	resp, err := client.Do(req)
	if err != nil {
		return classifyNetworkError("checkout_approve_network_error", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return &APIError{Code: "checkout_approve_failed", Message: fmt.Sprintf("ChatGPT approve failed: HTTP %d %s", resp.StatusCode, chatGPTErrorMessage(resp.Header, raw)), Status: 502}
	}
	var data map[string]any
	if json.Unmarshal(raw, &data) == nil && stringValue(data["result"]) != "" && !strings.EqualFold(stringValue(data["result"]), "approved") {
		return &APIError{Code: "checkout_approve_failed", Message: "ChatGPT approve unexpected result: " + stringValue(data["result"]), Status: 502}
	}
	return nil
}

func (e *Extractor) chatGPTPing(ctx context.Context, client httpDoer, token string) error {
	ping, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(e.Config.ChatGPTBase, "/")+"/backend-api/sentinel/ping", bytes.NewReader([]byte("{}")))
	if err == nil {
		setBrowserHeaders(ping)
		ping.Header.Set("Authorization", "Bearer "+token)
		ping.Header.Set("Content-Type", "application/json")
		ping.Header.Set("Origin", "https://chatgpt.com")
		ping.Header.Set("Referer", "https://chatgpt.com/")
		ping.Header.Set("X-OpenAI-Target-Path", "/backend-api/sentinel/ping")
		ping.Header.Set("X-OpenAI-Target-Route", "/backend-api/sentinel/ping")
		if resp, err := client.Do(ping); err == nil && resp != nil {
			_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1024))
			resp.Body.Close()
		} else if err != nil {
			return classifyNetworkError("checkout_ping_network_error", err)
		}
	}
	if err != nil {
		return err
	}
	return nil
}

func processorEntityForCountry(country, fallback string) string {
	if fallback != "" {
		return fallback
	}
	if strings.EqualFold(country, "US") {
		return "openai_llc"
	}
	return "openai_ie"
}

func (e *Extractor) pollStripePaymentPageRedirect(ctx context.Context, client httpDoer, cs, pk string) (string, error) {
	deadline := time.Now().Add(30 * time.Second)
	last := ""
	for time.Now().Before(deadline) {
		params := url.Values{
			"elements_session_client[client_betas][0]":                        {"custom_checkout_server_updates_1"},
			"elements_session_client[client_betas][1]":                        {"custom_checkout_manual_approval_1"},
			"elements_session_client[elements_init_source]":                   {"custom_checkout"},
			"elements_session_client[referrer_host]":                          {"chatgpt.com"},
			"elements_session_client[session_id]":                             {"elements_session_" + randomHex(6)},
			"elements_session_client[stripe_js_id]":                           {randomUUIDLike()},
			"elements_session_client[locale]":                                 {"en"},
			"elements_session_client[is_aggregation_expected]":                {"false"},
			"elements_options_client[saved_payment_method][enable_save]":      {"never"},
			"elements_options_client[saved_payment_method][enable_redisplay]": {"never"},
			"key":             {pk},
			"_stripe_version": {stripeVersionFull},
		}
		endpoint := strings.TrimRight(e.Config.StripeBase, "/") + "/v1/payment_pages/" + cs + "?" + params.Encode()
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
		if err != nil {
			return "", err
		}
		setStripeAPIHeaders(req)
		req.Header.Set("Accept", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			return "", classifyNetworkError("stripe_payment_page_network_error", err)
		}
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
		resp.Body.Close()
		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			var payload map[string]any
			if json.Unmarshal(raw, &payload) == nil {
				if pmURL := redirectURLFromPayload(payload); pmURL != "" {
					return e.resolveExternalRedirect(ctx, client, pmURL), nil
				}
				last = confirmResponseSummary(raw)
			}
		} else {
			last = fmt.Sprintf("HTTP %d %s", resp.StatusCode, stripeErrorMessage(raw))
		}
		select {
		case <-ctx.Done():
			return "", &APIError{Code: "client_timeout", Message: ctx.Err().Error(), Status: 504}
		case <-time.After(time.Second):
		}
	}
	return "", &APIError{Code: "stripe_confirm_failed", Message: "redirect url resolution timeout: " + last, Status: 502}
}

func (e *Extractor) resolveExternalRedirect(ctx context.Context, client httpDoer, redirectURL string) string {
	current := strings.TrimSpace(redirectURL)
	for i := 0; i < 5; i++ {
		if current == "" || isPayPalAuthorizeURL(current) {
			return current
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, current, nil)
		if err != nil {
			return current
		}
		setBrowserHeaders(req)
		resp, err := client.Do(req)
		if err != nil {
			return current
		}
		location := strings.TrimSpace(resp.Header.Get("Location"))
		status := resp.StatusCode
		resp.Body.Close()
		if status != 301 && status != 302 && status != 303 && status != 307 && status != 308 {
			return current
		}
		if location == "" {
			return current
		}
		base, err := url.Parse(current)
		if err != nil {
			current = location
			continue
		}
		next, err := url.Parse(location)
		if err != nil {
			return current
		}
		current = base.ResolveReference(next).String()
	}
	return current
}

func (e *Extractor) preConfirmPayPal(ctx context.Context, client httpDoer, pk, cs string, init map[string]any) error {
	payload, err := e.stripeForm(ctx, client, http.MethodPost, strings.TrimRight(e.Config.StripeBase, "/")+"/v1/payment_pages/"+cs+"/pre_confirm", url.Values{
		"eid":                 {"NA"},
		"payment_method_type": {"paypal"},
		"key":                 {pk},
	}, stringValue(firstNonEmpty(init["hosted_checkout_url"], init["url"], "https://pay.openai.com/c/pay/"+cs)))
	if err != nil {
		return err
	}
	if len(payload) > 0 {
		for _, key := range []string{"init_checksum", "total_summary", "invoice", "line_item_group", "payment_method_types", "ordered_payment_method_types", "payment_method_specs"} {
			if payload[key] != nil {
				init[key] = payload[key]
			}
		}
	}
	return nil
}

func confirmCountries(configCountry string, init map[string]any) []string {
	geoCountry := strings.ToUpper(stringValue(mapValueAny(init, "geocoding", "country_code")))
	return uniqueStrings("JP", geoCountry, strings.ToUpper(configCountry), "US")
}

func (e *Extractor) payPalConfirmForm(pk, cs string, init map[string]any, amounts map[string]int64, amountDue int64, returnURL, referrerURL, country string) url.Values {
	addr := billingAddress(country)
	expectedAmount, expectedOnBCA, hasExpectedOnBCA := confirmExpectedAmounts(init, amountDue)
	form := url.Values{
		"eid":                          {"NA"},
		"key":                          {pk},
		"init_checksum":                {stringValue(init["init_checksum"])},
		"expected_amount":              {fmt.Sprint(expectedAmount)},
		"expected_payment_method_type": {"paypal"},
		"payment_method_data[type]":    {"paypal"},
		"payment_method_data[billing_details][email]":                   {stringValue(init["customer_email"])},
		"payment_method_data[billing_details][address][country]":        {addr.Country},
		"payment_method_data[billing_details][address][postal_code]":    {addr.PostalCode},
		"payment_method_data[billing_details][address][state]":          {addr.State},
		"payment_method_data[billing_details][address][city]":           {addr.City},
		"payment_method_data[billing_details][address][line1]":          {addr.Line1},
		"payment_method_data[billing_details][address][line2]":          {addr.Line2},
		"consent[terms_of_service]":                                     {"accepted"},
		"last_displayed_line_item_group_details[subtotal]":              {fmt.Sprint(amounts["subtotal"])},
		"last_displayed_line_item_group_details[total_exclusive_tax]":   {fmt.Sprint(amounts["total_exclusive_tax"])},
		"last_displayed_line_item_group_details[total_inclusive_tax]":   {fmt.Sprint(amounts["total_inclusive_tax"])},
		"last_displayed_line_item_group_details[total_discount_amount]": {fmt.Sprint(amounts["total_discount_amount"])},
		"last_displayed_line_item_group_details[shipping_rate_amount]":  {fmt.Sprint(amounts["shipping_rate_amount"])},
		"return_url": {returnURL},
	}
	if hasExpectedOnBCA {
		form.Set("expected_amount_on_bca", fmt.Sprint(expectedOnBCA))
	}
	if referrerURL != "" {
		form.Set("referrer", referrerURL)
	}
	return form
}

func (e *Extractor) createPayPalPaymentMethod(ctx context.Context, client httpDoer, pk, cs string, init map[string]any, country string) (string, error) {
	addr := billingAddress(country)
	form := url.Values{
		"type":                                                       {"paypal"},
		"billing_details[email]":                                     {stringValue(init["customer_email"])},
		"billing_details[address][country]":                          {addr.Country},
		"billing_details[address][postal_code]":                      {addr.PostalCode},
		"billing_details[address][state]":                            {addr.State},
		"billing_details[address][city]":                             {addr.City},
		"billing_details[address][line1]":                            {addr.Line1},
		"billing_details[address][line2]":                            {addr.Line2},
		"key":                                                        {pk},
		"payment_user_agent":                                         {stripeCheckoutPaymentUserAgent()},
		"client_attribution_metadata[checkout_session_id]":           {cs},
		"client_attribution_metadata[merchant_integration_source]":   {"checkout"},
		"client_attribution_metadata[merchant_integration_subtype]":  {"hosted"},
		"client_attribution_metadata[merchant_integration_version]":  {"hosted_checkout"},
		"client_attribution_metadata[payment_method_selection_flow]": {"automatic"},
	}
	if configID := stringValue(init["config_id"]); configID != "" {
		form.Set("client_attribution_metadata[checkout_config_id]", configID)
	}
	endpoint := strings.TrimRight(e.Config.StripeBase, "/") + "/v1/payment_methods"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	setBrowserHeaders(req)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Origin", "https://pay.openai.com")
	req.Header.Set("Referer", strings.Split(stringValue(firstNonEmpty(init["hosted_checkout_url"], init["url"], "https://pay.openai.com/c/pay/"+cs)), "#")[0])
	resp, err := client.Do(req)
	if err != nil {
		return "", classifyNetworkError("stripe_payment_method_network_error", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	var data map[string]any
	if json.Unmarshal(raw, &data) != nil {
		return "", &APIError{Code: "stripe_payment_method_invalid_json", Message: "Stripe payment_method 响应不是 JSON", Status: 502}
	}
	pmID := stringValue(data["id"])
	if resp.StatusCode < 200 || resp.StatusCode >= 300 || !strings.HasPrefix(pmID, "pm_") {
		log.Printf("stripe payment_method create failed status=%d summary=%s", resp.StatusCode, sanitizeErrorSnippet(stripeErrorMessage(raw), 220))
		return "", &APIError{Code: "stripe_payment_method_failed", Message: fmt.Sprintf("Stripe payment_method failed: HTTP %d %s", resp.StatusCode, stripeErrorMessage(raw)), Status: 502}
	}
	return pmID, nil
}

func (e *Extractor) payPalConfirmWithPMForm(pk, cs string, init map[string]any, amounts map[string]int64, amountDue int64, returnURL, referrerURL, paymentMethod string) url.Values {
	expectedAmount, expectedOnBCA, hasExpectedOnBCA := confirmExpectedAmounts(init, amountDue)
	form := url.Values{
		"eid":                          {"NA"},
		"key":                          {pk},
		"init_checksum":                {stringValue(init["init_checksum"])},
		"expected_amount":              {fmt.Sprint(expectedAmount)},
		"expected_payment_method_type": {"paypal"},
		"payment_method":               {paymentMethod},
		"consent[terms_of_service]":    {"accepted"},
		"last_displayed_line_item_group_details[subtotal]":              {fmt.Sprint(amounts["subtotal"])},
		"last_displayed_line_item_group_details[total_exclusive_tax]":   {fmt.Sprint(amounts["total_exclusive_tax"])},
		"last_displayed_line_item_group_details[total_inclusive_tax]":   {fmt.Sprint(amounts["total_inclusive_tax"])},
		"last_displayed_line_item_group_details[total_discount_amount]": {fmt.Sprint(amounts["total_discount_amount"])},
		"last_displayed_line_item_group_details[shipping_rate_amount]":  {fmt.Sprint(amounts["shipping_rate_amount"])},
		"return_url": {returnURL},
	}
	if hasExpectedOnBCA {
		form.Set("expected_amount_on_bca", fmt.Sprint(expectedOnBCA))
	}
	if referrerURL != "" {
		form.Set("referrer", referrerURL)
	}
	form.Set("js_checksum", stripeShiftChecksum(map[string]string{"id": paymentMethod}))
	form.Set("rv_timestamp", stripeShiftChecksum(map[string]string{
		"rvTs": "2024-01-01 00:00:00 -0000",
		"rv":   "0711c6012fed57bca21a1857e407d89e5745e3df",
		"sv":   "d616dee94d164fb10d46843aec403d3beeb078d097c08dd8d77451247f21d924",
	}))
	form.Set("client_attribution_metadata[checkout_session_id]", cs)
	form.Set("client_attribution_metadata[merchant_integration_source]", "checkout")
	form.Set("client_attribution_metadata[merchant_integration_subtype]", "hosted")
	form.Set("client_attribution_metadata[merchant_integration_version]", "hosted_checkout")
	form.Set("client_attribution_metadata[payment_method_selection_flow]", "automatic")
	if configID := stringValue(init["config_id"]); configID != "" {
		form.Set("client_attribution_metadata[checkout_config_id]", configID)
	}
	return form
}

func stripeCheckoutPaymentUserAgent() string {
	const stripeJSBuild = "0711c6012f"
	return "stripe.js/" + stripeJSBuild + "; stripe-js-v3/" + stripeJSBuild + "; checkout"
}

type billingAddressValue struct {
	Country    string
	PostalCode string
	State      string
	City       string
	Line1      string
	Line2      string
}

func billingAddress(country string) billingAddressValue {
	switch strings.ToUpper(strings.TrimSpace(country)) {
	case "JP":
		return billingAddressValue{
			Country:    "JP",
			PostalCode: "100-0001",
			State:      "東京都",
			City:       "千代田区",
			Line1:      "1-1 Chiyoda",
			Line2:      "Tokyo",
		}
	case "DE":
		return billingAddressValue{
			Country:    "DE",
			PostalCode: "10115",
			State:      "Berlin",
			City:       "Berlin",
			Line1:      "Invalidenstrasse 1",
			Line2:      "Berlin",
		}
	default:
		return billingAddressValue{
			Country:    "US",
			PostalCode: "10001",
			State:      "NY",
			City:       "New York",
			Line1:      "350 5th Ave",
			Line2:      "New York",
		}
	}
}

func confirmChecksumID(init map[string]any, cs string) string {
	if id := stringValue(init["id"]); id != "" {
		return id
	}
	if id := stringValue(init["payment_page_id"]); id != "" {
		return id
	}
	return cs
}

func confirmExpectedAmounts(init map[string]any, fallbackAmount int64) (expectedAmount int64, expectedOnBCA int64, hasExpectedOnBCA bool) {
	expectedAmount = fallbackAmount
	totalDue, hasTotalDue := int64(0), false
	if totalSummary, _ := init["total_summary"].(map[string]any); totalSummary != nil {
		totalDue, hasTotalDue = int64Value(totalSummary["due"])
		if hasTotalDue {
			expectedAmount = totalDue
		}
	}
	if lineItem, _ := init["line_item_group"].(map[string]any); lineItem != nil {
		if total, ok := int64Value(lineItem["total"]); ok {
			expectedAmount = total
		}
		if auto, _ := lineItem["automatic_surcharge_settings"].(map[string]any); boolValue(auto["enabled"]) {
			if totalSummary, _ := init["total_summary"].(map[string]any); totalSummary != nil {
				if due, ok := int64Value(totalSummary["due"]); ok {
					expectedAmount = due
				}
			}
		}
	}
	if invoice, _ := init["invoice"].(map[string]any); invoice != nil {
		if amount, ok := int64Value(invoice["amount_due"]); ok {
			if stringValue(invoice["billing_cycle_anchor"]) != "" && !boolValue(invoice["has_prorations"]) {
				if hasTotalDue {
					return totalDue, amount, true
				}
				return 0, amount, true
			}
			if hasTotalDue {
				return expectedAmount, 0, false
			}
			expectedAmount = amount
		}
	}
	return expectedAmount, 0, false
}

func confirmReturnURLForInit(init map[string]any, cs string) string {
	mode := stringValue(firstNonEmpty(init["ui_mode"], "hosted"))
	raw := rawConfirmReturnURLForInit(init, cs)
	return confirmReturnURLWithMode(raw, mode)
}

func rawConfirmReturnURLForInit(init map[string]any, cs string) string {
	raw := stringValue(firstNonEmpty(init["url"], init["hosted_checkout_url"], init["stripe_hosted_url"], init["return_url"], "https://pay.openai.com/c/pay/"+cs))
	return toOpenAIPayURL(raw)
}

func checkoutReferrerURL(init map[string]any, cs string) string {
	raw := stringValue(firstNonEmpty(init["hosted_checkout_url"], init["stripe_hosted_url"], init["url"], "https://pay.openai.com/c/pay/"+cs))
	return strings.Split(toOpenAIPayURL(raw), "#")[0]
}

func confirmReturnURL(raw string) string {
	return confirmReturnURLWithMode(raw, "hosted")
}

func confirmReturnURLWithMode(raw, uiMode string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return raw
	}
	uiMode = strings.TrimSpace(uiMode)
	if uiMode == "" {
		uiMode = "hosted"
	}
	base := raw
	fragment := ""
	if idx := strings.Index(base, "#"); idx >= 0 {
		fragment = base[idx:]
		base = base[:idx]
	}
	withQuery := appendQueryParams(base, map[string]string{
		"redirect_pm_type": "paypal",
		"lid":              randomAlphaNum(24),
		"ui_mode":          uiMode,
	})
	return withQuery + fragment
}

func appendQueryParams(raw string, params map[string]string) string {
	u, err := url.Parse(raw)
	if err != nil {
		sep := "?"
		if strings.Contains(raw, "?") {
			sep = "&"
		}
		q := url.Values{}
		for key, value := range params {
			if value != "" {
				q.Set(key, value)
			}
		}
		if encoded := q.Encode(); encoded != "" {
			return raw + sep + encoded
		}
		return raw
	}
	q := u.Query()
	for key, value := range params {
		if value != "" && q.Get(key) == "" {
			q.Set(key, value)
		}
	}
	u.RawQuery = q.Encode()
	return u.String()
}

func stripeShiftChecksum(value map[string]string) string {
	raw, _ := json.Marshal(value)
	encoded := stripeEncodeChecksumPlaintext(string(raw))
	if encoded == "" {
		return ""
	}
	out := make([]byte, 0, len(raw))
	for _, ch := range encoded {
		if ch < 32 || ch > 126 {
			out = append(out, string(ch)...)
			continue
		}
		out = append(out, byte((int(ch)-32+11)%95+32))
	}
	return string(out)
}

func stripeEncodeChecksumPlaintext(raw string) string {
	if raw == "" {
		return ""
	}
	xored := make([]byte, len(raw))
	for i := range raw {
		xored[i] = raw[i] ^ 5
	}
	pad := 3 - (len(raw) % 3)
	if pad > 0 {
		for i := 0; i < pad; i++ {
			xored = append(xored, byte(' ')^5)
		}
	}
	return url.QueryEscape(base64.StdEncoding.EncodeToString(xored))
}

func (e *Extractor) postConfirmPayPal(ctx context.Context, client httpDoer, cs string, form url.Values) (string, error) {
	endpoint := strings.TrimRight(e.Config.StripeBase, "/") + "/v1/payment_pages/" + cs + "/confirm"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	setBrowserHeaders(req)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Origin", "https://pay.openai.com")
	referer := form.Get("referrer")
	if referer == "" {
		referer = strings.Split(form.Get("return_url"), "#")[0]
	}
	req.Header.Set("Referer", referer)
	resp, err := client.Do(req)
	if err != nil {
		return "", classifyNetworkError("stripe_confirm_network_error", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	pm := firstUsefulRedirectURL(raw)
	if pm == "" {
		var data any
		if json.Unmarshal(raw, &data) == nil {
			pm = findNestedURL(data, pmRedirectRe)
		}
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 || pm == "" {
		log.Printf("stripe confirm no paypal redirect status=%d %s", resp.StatusCode, confirmDebugSummary(raw))
		return "", &APIError{Code: "stripe_confirm_failed", Message: fmt.Sprintf("Stripe confirm failed: HTTP %d %s %s", resp.StatusCode, confirmResponseSummary(raw), stripeErrorMessage(raw)), Status: 502}
	}
	return pm, nil
}

func confirmDebugSummary(raw []byte) string {
	var data map[string]any
	if json.Unmarshal(raw, &data) != nil {
		return "debug=non_json"
	}
	keys := make([]string, 0, len(data))
	for key := range data {
		keys = append(keys, key)
	}
	urls := []string{}
	for _, found := range findNestedURLs(data, anyURLRe, nil) {
		if item := urlDebugItem(found); item != "" {
			urls = append(urls, item)
		}
		if len(urls) >= 12 {
			break
		}
	}
	return fmt.Sprintf("debug={keys:%s next_action:%s status:%s urls:%s}",
		strings.Join(keys, ","),
		stringValue(mapValueAny(data, "next_action", "type")),
		stringValue(firstNonEmpty(data["status"], mapValueAny(data, "setup_intent", "status"))),
		strings.Join(urls, ","))
}

func urlDebugItem(raw string) string {
	u, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	host := strings.ToLower(u.Hostname())
	path := u.EscapedPath()
	if host == "" {
		return ""
	}
	if len(path) > 80 {
		path = path[:80]
	}
	return host + path
}

func confirmResponseSummary(raw []byte) string {
	var data map[string]any
	if json.Unmarshal(raw, &data) != nil {
		return "summary=non_json"
	}
	setupIntent, _ := data["setup_intent"].(map[string]any)
	paymentIntent, _ := data["payment_intent"].(map[string]any)
	nextAction := ""
	if setupIntent != nil {
		nextAction = stringValue(mapValueAny(setupIntent, "next_action", "type"))
		if nextAction == "" {
			nextAction = stringValue(setupIntent["status"])
		}
	}
	paymentNextAction := ""
	paymentIntentStatus := ""
	if paymentIntent != nil {
		paymentNextAction = stringValue(mapValueAny(paymentIntent, "next_action", "type"))
		paymentIntentStatus = stringValue(paymentIntent["status"])
	}
	methods := compactStringSlice(data["payment_method_types"])
	ordered := compactStringSlice(data["ordered_payment_method_types"])
	specs := paymentMethodSpecTypes(data["payment_method_specs"])
	merchant := stringValue(firstNonEmpty(data["merchant_id"], mapValueAny(data, "account_settings", "account_id")))
	if merchant != "" && len(merchant) > 16 {
		merchant = merchant[:16] + "..."
	}
	return fmt.Sprintf("summary={merchant:%s setup_intent:%t setup_next:%s payment_intent:%t payment_status:%s payment_next:%s methods:%s ordered:%s specs:%s}",
		merchant, setupIntent != nil, nextAction, paymentIntent != nil, paymentIntentStatus, paymentNextAction, methods, ordered, specs)
}

func compactStringSlice(value any) string {
	items, _ := value.([]any)
	if len(items) == 0 {
		return "-"
	}
	out := make([]string, 0, len(items))
	for _, item := range items {
		if s := stringValue(item); s != "" {
			out = append(out, s)
		}
	}
	if len(out) == 0 {
		return "-"
	}
	return strings.Join(out, ",")
}

func paymentMethodSpecTypes(value any) string {
	items, _ := value.([]any)
	if len(items) == 0 {
		return "-"
	}
	out := make([]string, 0, len(items))
	for _, item := range items {
		obj, _ := item.(map[string]any)
		if obj == nil {
			continue
		}
		if s := stringValue(obj["type"]); s != "" {
			out = append(out, s)
		}
	}
	if len(out) == 0 {
		return "-"
	}
	return strings.Join(out, ",")
}

func mapValueAny(root map[string]any, key, child string) any {
	parent, _ := root[key].(map[string]any)
	if parent == nil {
		return nil
	}
	return parent[child]
}

func firstUsefulRedirectURL(raw []byte) string {
	text := strings.ReplaceAll(string(raw), `\/`, `/`)
	for _, candidate := range anyURLRe.FindAllString(text, -1) {
		if isPayPalAuthorizeURL(candidate) {
			return candidate
		}
	}
	var data any
	if json.Unmarshal(raw, &data) == nil {
		for _, found := range findNestedURLs(data, anyURLRe, nil) {
			if isPayPalAuthorizeURL(found) {
				return found
			}
		}
	}
	return ""
}

func existingPayPalAuthorizeURL(init map[string]any) string {
	raw, err := json.Marshal(init)
	if err != nil {
		return ""
	}
	return firstUsefulRedirectURL(raw)
}

func isPayPalAuthorizeURL(candidate string) bool {
	u, err := url.Parse(candidate)
	if err != nil {
		return false
	}
	host := strings.ToLower(u.Hostname())
	path := strings.ToLower(u.EscapedPath())
	full := strings.ToLower(candidate)
	if strings.Contains(host, "js.stripe.com") ||
		strings.Contains(host, "pm-hooks.stripe.com") ||
		strings.Contains(path, "apple_pay") ||
		strings.Contains(path, "merchant_token") ||
		strings.Contains(path, "/img/") ||
		strings.Contains(path, "/fingerprinted/") {
		return false
	}
	if strings.HasSuffix(host, "stripe.com") &&
		(strings.Contains(full, "redirect") || strings.Contains(full, "authorize") || strings.Contains(full, "paypal")) {
		return true
	}
	if strings.Contains(host, "paypal.com") {
		return true
	}
	return false
}

func stripeErrorMessage(raw []byte) string {
	var data map[string]any
	if json.Unmarshal(raw, &data) != nil {
		text := string(raw)
		if len(text) > 240 {
			text = text[:240]
		}
		return text
	}
	errObj, _ := data["error"].(map[string]any)
	msg := stringValue(errObj["message"])
	if msg == "" {
		parts := []string{}
		for _, key := range []string{"type", "code", "param", "decline_code"} {
			if value := stringValue(errObj[key]); value != "" {
				parts = append(parts, key+"="+value)
			}
		}
		msg = strings.Join(parts, " ")
	}
	if len(msg) > 240 {
		msg = msg[:240]
	}
	if msg == "" {
		compact, _ := json.Marshal(data)
		msg = string(compact)
		msg = sanitizeErrorSnippet(msg, 500)
	}
	return msg
}

func sanitizeErrorSnippet(text string, limit int) string {
	text = anyURLRe.ReplaceAllString(text, "<url>")
	if len(text) > limit {
		return text[:limit]
	}
	return text
}

func setBrowserHeaders(req *http.Request) {
	deviceID := browserDeviceID(req.Context())
	req.Header = http.Header{
		"Accept":             {"application/json, text/plain, */*"},
		"Accept-Language":    {"en-US,en;q=0.9"},
		"User-Agent":         {stripeAPIUserAgent},
		"Sec-CH-UA":          {`"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"`},
		"Sec-CH-UA-Platform": {`"Windows"`},
		"Sec-CH-UA-Mobile":   {"?0"},
		"Oai-Device-Id":      {deviceID},
		"Oai-Language":       {"en-US"},
		"Sec-Fetch-Dest":     {"empty"},
		"Sec-Fetch-Mode":     {"cors"},
		"Sec-Fetch-Site":     {"same-origin"},
		"Cookie":             {"oai-did=" + deviceID},
		http.HeaderOrderKey:  {"accept", "accept-language", "user-agent", "sec-ch-ua", "sec-ch-ua-platform", "sec-ch-ua-mobile", "oai-device-id", "oai-language", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "cookie"},
		http.PHeaderOrderKey: {":method", ":authority", ":scheme", ":path"},
	}
}

func setStripeAPIHeaders(req *http.Request) {
	req.Header = http.Header{
		"Accept":             {"application/json"},
		"Accept-Language":    {"en-US,en;q=0.9"},
		"User-Agent":         {stripeAPIUserAgent},
		http.HeaderOrderKey:  {"accept", "accept-language", "user-agent"},
		http.PHeaderOrderKey: {":method", ":authority", ":scheme", ":path"},
	}
}

func findNestedURL(value any, pattern *regexp.Regexp) string {
	switch v := value.(type) {
	case string:
		return pattern.FindString(v)
	case map[string]any:
		for _, key := range []string{"url", "redirect_url", "authorize_url", "hosted_checkout_url", "checkout_url"} {
			if found := findNestedURL(v[key], pattern); found != "" {
				return found
			}
		}
		for _, child := range v {
			if found := findNestedURL(child, pattern); found != "" {
				return found
			}
		}
	case []any:
		for _, child := range v {
			if found := findNestedURL(child, pattern); found != "" {
				return found
			}
		}
	}
	return ""
}

func findNestedURLs(value any, pattern *regexp.Regexp, out []string) []string {
	switch v := value.(type) {
	case string:
		out = append(out, pattern.FindAllString(v, -1)...)
	case map[string]any:
		for _, key := range []string{"url", "redirect_url", "authorize_url", "hosted_checkout_url", "checkout_url"} {
			out = findNestedURLs(v[key], pattern, out)
		}
		for _, child := range v {
			out = findNestedURLs(child, pattern, out)
		}
	case []any:
		for _, child := range v {
			out = findNestedURLs(child, pattern, out)
		}
	}
	return out
}

func int64Value(value any) (int64, bool) {
	switch v := value.(type) {
	case float64:
		return int64(v), v == float64(int64(v))
	case int:
		return int64(v), true
	case int64:
		return v, true
	case string:
		var out int64
		_, err := fmt.Sscan(v, &out)
		return out, err == nil
	default:
		return 0, false
	}
}

func int64Ptr(value any) *int64 {
	out, ok := int64Value(value)
	if !ok {
		return nil
	}
	return &out
}

func boolValue(value any) bool {
	switch v := value.(type) {
	case bool:
		return v
	case string:
		return strings.EqualFold(v, "true")
	default:
		return false
	}
}

func stringValue(value any) string {
	switch v := value.(type) {
	case string:
		return v
	case fmt.Stringer:
		return v.String()
	case nil:
		return ""
	default:
		return fmt.Sprint(v)
	}
}

func firstNonEmpty(values ...any) any {
	for _, value := range values {
		if stringValue(value) != "" {
			return value
		}
	}
	return ""
}

func displayAmounts(init map[string]any) map[string]int64 {
	invoice, _ := init["invoice"].(map[string]any)
	totalSummary, _ := init["total_summary"].(map[string]any)
	due, _ := int64Value(firstNonEmpty(totalSummary["due"], invoice["amount_due"], 0))
	total, _ := int64Value(firstNonEmpty(totalSummary["total"], invoice["amount_due"], due))
	subtotal, _ := int64Value(firstNonEmpty(totalSummary["subtotal"], total))
	exclusiveTax := sumStripeAmountList(invoice["total_tax_amounts"], "exclusive")
	inclusiveTax := sumStripeAmountList(invoice["total_tax_amounts"], "inclusive")
	discount := subtotal - total
	if discount < 0 {
		discount = 0
	}
	return map[string]int64{
		"subtotal":              subtotal,
		"total_exclusive_tax":   exclusiveTax,
		"total_inclusive_tax":   inclusiveTax,
		"total_discount_amount": discount,
		"shipping_rate_amount":  0,
		"due":                   due,
	}
}

func sumStripeAmountList(value any, taxability string) int64 {
	items, _ := value.([]any)
	var out int64
	for _, item := range items {
		obj, _ := item.(map[string]any)
		if obj == nil {
			continue
		}
		if taxability != "" {
			behavior := strings.ToLower(stringValue(firstNonEmpty(obj["taxability_reason"], obj["tax_behavior"], obj["inclusive"])))
			if taxability == "inclusive" && !strings.Contains(behavior, "inclusive") {
				continue
			}
			if taxability == "exclusive" && strings.Contains(behavior, "inclusive") {
				continue
			}
		}
		amount, ok := int64Value(firstNonEmpty(obj["amount"], obj["tax_amount"], 0))
		if ok {
			out += amount
		}
	}
	return out
}

func amountDisplay(amount *int64, currency string) string {
	if amount == nil {
		return "unknown"
	}
	code := strings.ToUpper(currency)
	if code == "" {
		code = "UNKNOWN"
	}
	if zeroDecimal[strings.ToLower(currency)] {
		return fmt.Sprintf("%d %s", *amount, code)
	}
	return fmt.Sprintf("%.2f %s", float64(*amount)/100, code)
}

func codeOf(err error) string {
	if api, ok := err.(*APIError); ok {
		return api.Code
	}
	if isProxyDNSError(err) {
		return "proxy_dns_error"
	}
	return "extract_failed"
}

func isProxyDNSError(err error) bool {
	if err == nil {
		return false
	}
	text := err.Error()
	return strings.Contains(text, "lookup gate-") && strings.Contains(text, "no such host")
}

func contentTypeBase(header string) string {
	base, _, err := mime.ParseMediaType(header)
	if err != nil {
		return header
	}
	return base
}
