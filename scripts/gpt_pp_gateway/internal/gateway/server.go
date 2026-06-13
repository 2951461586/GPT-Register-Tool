package gateway

import (
	"context"
	"encoding/json"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"runtime/debug"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

const defaultExtractConcurrency = 128
const defaultBatchConcurrency = 12
const activeVisitorWindow = 60 * time.Second

type Server struct {
	Extractor *Extractor
	counter   atomic.Int64
	countFile string
	countMu   sync.Mutex
	extractQ  chan struct{}
	visitors  map[string]time.Time
	visitMu   sync.Mutex
}

func NewServer(extractor *Extractor) *Server {
	s := &Server{
		Extractor: extractor,
		countFile: "webapp/counter.json",
		extractQ:  make(chan struct{}, defaultExtractConcurrency),
		visitors:  make(map[string]time.Time),
	}
	s.counter.Store(s.loadCounter())
	return s
}

func (s *Server) Register(mux *http.ServeMux) {
	mux.HandleFunc("/api/health", s.health)
	mux.HandleFunc("/api/stats", s.stats)
	mux.HandleFunc("/api/extract", s.extract)
	mux.HandleFunc("/api/extract-batch", s.extractBatch)
	mux.HandleFunc("/api/test-proxy", s.testProxy)
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":                  true,
		"runtime":             "go",
		"extract_queue_limit": cap(s.extractQ),
		"extract_queue_used":  len(s.extractQ),
		"batch_concurrency":   defaultBatchConcurrency,
		"online_count":        s.onlineCount(),
	})
}

func (s *Server) stats(w http.ResponseWriter, r *http.Request) {
	s.recordVisitor(r)
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":            true,
		"success_count": s.counter.Load(),
		"online_count":  s.onlineCount(),
	})
}

func (s *Server) extract(w http.ResponseWriter, r *http.Request) {
	defer func() {
		if rec := recover(); rec != nil {
			log.Printf("extract panic: %v\n%s", rec, string(debug.Stack()))
			writeJSON(w, http.StatusBadGateway, map[string]any{"ok": false, "code": "extract_panic", "message": "转链内部异常，已保护恢复，请重试"})
		}
	}()
	s.recordVisitor(r)
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"ok": false, "code": "method_not_allowed"})
		return
	}
	var req struct {
		Credential  string `json:"credential"`
		AccessToken string `json:"accessToken"`
		Proxy       string `json:"proxy"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 96*1024)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "code": "invalid_json", "message": "请求 JSON 无法解析"})
		return
	}
	token := req.AccessToken
	if token == "" {
		token = req.Credential
	}
	select {
	case s.extractQ <- struct{}{}:
		defer func() { <-s.extractQ }()
	case <-r.Context().Done():
		writeJSON(w, http.StatusGatewayTimeout, map[string]any{"ok": false, "code": "client_timeout", "message": "请求已取消或等待队列超时"})
		return
	}
	extractCtx, cancel := context.WithTimeout(context.Background(), s.extractTimeout())
	defer cancel()
	result, err := s.Extractor.Extract(extractCtx, token, req.Proxy)
	status := http.StatusOK
	if err != nil {
		status = statusOf(err)
	}
	if err != nil || result == nil || !result.OK {
		code := codeOf(err)
		msg := ""
		amount := "unknown"
		elapsed := int64(0)
		if err != nil {
			msg = err.Error()
		}
		if result != nil {
			if result.Code != "" {
				code = result.Code
			}
			if result.Message != "" {
				msg = result.Message
			}
			if result.AmountDisplay != "" {
				amount = result.AmountDisplay
			}
			elapsed = result.ElapsedMS
		}
		log.Printf("extract failed status=%d code=%s amount=%s elapsed_ms=%d proxy=%t msg=%q", status, code, amount, elapsed, req.Proxy != "", msg)
		if result != nil && status >= http.StatusBadGateway {
			result.Message = publicExtractMessage(code)
		}
	}
	if result != nil && result.OK {
		s.incrementCounter()
	}
	writeJSON(w, status, result)
}

func (s *Server) extractBatch(w http.ResponseWriter, r *http.Request) {
	defer func() {
		if rec := recover(); rec != nil {
			log.Printf("extract batch panic: %v\n%s", rec, string(debug.Stack()))
		}
	}()
	s.recordVisitor(r)
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"ok": false, "code": "method_not_allowed"})
		return
	}
	var req struct {
		Credential  string   `json:"credential"`
		AccessToken string   `json:"accessToken"`
		Credentials []string `json:"credentials"`
		Tokens      []string `json:"tokens"`
		Proxy       string   `json:"proxy"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 2<<20)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "code": "invalid_json", "message": "请求 JSON 无法解析"})
		return
	}
	if _, err := NormalizeProxyCandidates(req.Proxy); err != nil {
		writeJSON(w, statusOf(err), map[string]any{"ok": false, "code": codeOf(err), "message": err.Error()})
		return
	}
	tokens, err := collectBatchTokens(req.Credential, req.AccessToken, req.Credentials, req.Tokens)
	if err != nil {
		writeJSON(w, statusOf(err), map[string]any{"ok": false, "code": codeOf(err), "message": err.Error()})
		return
	}
	if len(tokens) > 200 {
		tokens = tokens[:200]
	}

	w.Header().Set("Content-Type", "application/x-ndjson; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
	flusher, _ := w.(http.Flusher)

	type batchRow struct {
		Index  int            `json:"index"`
		OK     bool           `json:"ok"`
		Result *ExtractResult `json:"result,omitempty"`
		Code   string         `json:"code,omitempty"`
		Error  string         `json:"error,omitempty"`
	}

	jobs := make(chan int)
	rows := make(chan batchRow, len(tokens))
	workers := defaultBatchConcurrency
	if workers > len(tokens) {
		workers = len(tokens)
	}
	if workers < 1 {
		workers = 1
	}

	var wg sync.WaitGroup
	for worker := 0; worker < workers; worker++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for idx := range jobs {
				extractCtx, cancel := context.WithTimeout(context.Background(), s.extractTimeout())
				result, err := s.Extractor.Extract(extractCtx, tokens[idx], req.Proxy)
				cancel()
				row := batchRow{Index: idx + 1, Result: result}
				if result != nil && result.OK {
					row.OK = true
					s.incrementCounter()
				} else {
					row.Code = codeOf(err)
					if result != nil && result.Code != "" {
						row.Code = result.Code
					}
					if result != nil && result.Message != "" {
						row.Error = result.Message
					} else if err != nil {
						row.Error = err.Error()
					} else {
						row.Error = "本次未拿到有效 PayPal 授权链接"
					}
					if statusOf(err) >= http.StatusBadGateway {
						row.Error = publicExtractMessage(row.Code)
						if result != nil {
							result.Message = row.Error
						}
					}
					log.Printf("extract batch failed index=%d code=%s proxy=%t msg=%q", idx+1, row.Code, req.Proxy != "", sanitizeErrorSnippet(row.Error, 240))
				}
				rows <- row
			}
		}()
	}
	go func() {
		for idx := range tokens {
			jobs <- idx
		}
		close(jobs)
		wg.Wait()
		close(rows)
	}()

	enc := json.NewEncoder(w)
	for row := range rows {
		if err := enc.Encode(row); err != nil {
			log.Printf("extract batch stream closed: %v", err)
			return
		}
		if flusher != nil {
			flusher.Flush()
		}
	}
}

func (s *Server) testProxy(w http.ResponseWriter, r *http.Request) {
	s.recordVisitor(r)
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"ok": false, "code": "method_not_allowed"})
		return
	}
	var req struct {
		Proxy string `json:"proxy"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "code": "invalid_json", "message": "请求 JSON 无法解析"})
		return
	}
	candidates, err := NormalizeProxyCandidates(req.Proxy)
	if err != nil {
		writeJSON(w, statusOf(err), map[string]any{"ok": false, "code": codeOf(err), "message": err.Error()})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 8*time.Second)
	defer cancel()
	start := time.Now()
	lastCode := ""
	lastMessage := ""
	for _, candidate := range candidates {
		geo, err := s.Extractor.probeProxyGeo(ctx, candidate)
		if err == nil && geo != nil && geo.IP != "" {
			writeJSON(w, http.StatusOK, map[string]any{
				"ok":             true,
				"runtime":        "go",
				"ip":             geo.IP,
				"candidates":     len(candidates),
				"country":        geo.Country,
				"region":         geo.Region,
				"org":            geo.Org,
				"city":           geo.City,
				"latency_ms":     time.Since(start).Milliseconds(),
				"proxy_protocol": proxyScheme(candidate),
				"checkout_ready": true,
				"message":        "代理基础连通测试通过；真实提链以 /api/extract 端到端为准",
			})
			return
		}
		lastCode = codeOf(err)
		if err != nil {
			lastMessage = err.Error()
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":         false,
		"runtime":    "go",
		"code":       lastCode,
		"candidates": len(candidates),
		"message":    publicProxyTestMessage(lastCode, lastMessage),
	})
}

func (s *Server) recordVisitor(r *http.Request) {
	key := visitorKey(r)
	if key == "" {
		return
	}
	now := time.Now()
	s.visitMu.Lock()
	s.visitors[key] = now
	for key, seen := range s.visitors {
		if now.Sub(seen) > activeVisitorWindow {
			delete(s.visitors, key)
		}
	}
	s.visitMu.Unlock()
}

func collectBatchTokens(values ...any) ([]string, error) {
	seen := map[string]struct{}{}
	out := []string{}
	addRaw := func(raw string) {
		tokens, _ := ExtractAccessTokens(raw)
		for _, token := range tokens {
			if _, ok := seen[token]; ok {
				continue
			}
			seen[token] = struct{}{}
			out = append(out, token)
		}
	}
	for _, value := range values {
		switch v := value.(type) {
		case string:
			addRaw(v)
		case []string:
			for _, item := range v {
				addRaw(item)
			}
		}
	}
	if len(out) == 0 {
		return nil, &APIError{Code: "invalid_access_token", Message: "未识别到合法 JWT 格式 accessToken", Status: 400}
	}
	return out, nil
}

func (s *Server) extractTimeout() time.Duration {
	base := s.Extractor.Config.Timeout
	if base <= 0 {
		base = 10 * time.Second
	}
	attempts := s.Extractor.Config.MaxAttempts
	if attempts < 1 {
		attempts = 1
	}
	timeout := time.Duration(attempts+1)*base + 8*time.Second
	if timeout < 25*time.Second {
		timeout = 25 * time.Second
	}
	return timeout
}

func (s *Server) onlineCount() int {
	now := time.Now()
	s.visitMu.Lock()
	defer s.visitMu.Unlock()
	for key, seen := range s.visitors {
		if now.Sub(seen) > activeVisitorWindow {
			delete(s.visitors, key)
		}
	}
	return len(s.visitors)
}

func (s *Server) loadCounter() int64 {
	s.countMu.Lock()
	defer s.countMu.Unlock()
	raw, err := os.ReadFile(s.countFile)
	if err != nil {
		return 0
	}
	var data struct {
		SuccessCount int64 `json:"success_count"`
	}
	if json.Unmarshal(raw, &data) != nil || data.SuccessCount < 0 {
		return 0
	}
	return data.SuccessCount
}

func (s *Server) incrementCounter() int64 {
	s.countMu.Lock()
	defer s.countMu.Unlock()
	next := s.counter.Add(1)
	_ = os.MkdirAll(filepath.Dir(s.countFile), 0755)
	payload, _ := json.Marshal(map[string]int64{"success_count": next})
	_ = os.WriteFile(s.countFile, payload, 0644)
	return next
}

func clientIP(r *http.Request) string {
	for _, header := range []string{"CF-Connecting-IP", "X-Real-IP"} {
		value := strings.TrimSpace(r.Header.Get(header))
		if value != "" {
			return value
		}
	}
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		if first := strings.TrimSpace(strings.Split(xff, ",")[0]); first != "" {
			return first
		}
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err == nil {
		return host
	}
	return strings.TrimSpace(r.RemoteAddr)
}

func visitorKey(r *http.Request) string {
	if value := strings.TrimSpace(r.Header.Get("X-Visitor-ID")); value != "" {
		if len(value) > 96 {
			value = value[:96]
		}
		return "v:" + value
	}
	return "ip:" + clientIP(r)
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func statusOf(err error) int {
	if api, ok := err.(*APIError); ok && api.Status > 0 {
		return api.Status
	}
	return http.StatusBadGateway
}

func publicExtractMessage(code string) string {
	switch code {
	case "checkout_token_invalidated":
		return "ChatGPT checkout 返回 401/token_invalidated：这个 Access Token 已被上游作废，请重新导出最新 Access Token"
	case "checkout_unauthorized":
		return "ChatGPT checkout 返回 401：Access Token 未被 checkout 接口接受，请重新导出最新 Access Token"
	case "checkout_forbidden":
		return "ChatGPT checkout 返回 403：当前代理出口或账号会话被上游 checkout 风控拒绝，本次未拿到 PayPal 授权链接"
	case "checkout_rate_limited":
		return "ChatGPT checkout 返回 429：上游限流，请稍后或降低并发重试"
	case "proxy_connect_rejected":
		return "代理 HTTP CONNECT 被拒绝；该节点基础 IP 可用但 HTTPS 隧道不可用，系统已尝试 SOCKS 分支"
	case "checkout_network_error", "stripe_checkout_network_error", "stripe_init_network_error", "stripe_confirm_network_error":
		return "上游网络短暂拥塞，本次未拿到有效 PayPal 授权链接"
	case "checkout_no_hosted_url":
		return "ChatGPT checkout 未返回 hosted checkout URL，本次无法继续到 Stripe/PayPal"
	case "stripe_confirm_failed":
		return "Stripe confirm 未返回 PayPal authorize 链接，本次未拿到有效 PayPal 授权链接"
	case "stripe_init_failed", "stripe_init_invalid_json", "stripe_publishable_key_missing":
		return "Stripe checkout 初始化失败，本次未拿到有效 PayPal 授权链接"
	default:
		return "本次未拿到有效 PayPal 授权链接"
	}
}

func publicProxyTestMessage(code, detail string) string {
	switch code {
	case "proxy_dns_error":
		return "代理服务器域名解析失败，请核对域名和端口"
	case "proxy_ip_probe_network":
		return "代理连接失败或超时，请确认账号、密码、端口和白名单"
	case "proxy_ip_probe_failed":
		return "代理已连接但未返回可用出口 IP，请更换节点后重试"
	default:
		if detail != "" {
			return "代理测试失败：" + sanitizeErrorSnippet(detail, 180)
		}
		return "代理测试失败，请更换节点后重试"
	}
}
