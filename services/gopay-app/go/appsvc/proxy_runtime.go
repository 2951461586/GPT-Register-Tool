package appsvc

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/byte-v-forge/gpt/gopay/protocol"
)

type proxyRuntimeSessionResponse struct {
	Session struct {
		SessionID string `json:"session_id"`
	} `json:"session"`
	Pool struct {
		Endpoints []map[string]any `json:"endpoints"`
	} `json:"pool"`
}

func (s *Server) createProxyRuntimeSession(ctx context.Context) (map[string]any, error) {
	baseURL := strings.TrimRight(strings.TrimSpace(s.cfg.ProxyRuntimeHTTPAddr), "/")
	if baseURL == "" {
		return nil, nil
	}
	reqCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()

	body := []byte(`{"force_new":true,"policy":{"mode":"PROXY_SESSION_MODE_STICKY","region":"ID","labels":{"purpose":"gopay_device_proxy","rotation":"active"},"upstream_kind":"PROXY_UPSTREAM_KIND_DYNAMIC_IP","rotation_mode":"PROXY_ROTATION_MODE_STICKY_SESSION"}}`)
	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost, baseURL+"/api/proxy-runtime/session/new", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("proxy-runtime session/new: %w", err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("read proxy-runtime session/new response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("proxy-runtime session/new failed: status %d %s", resp.StatusCode, protocol.Snippet(protocol.RedactText(string(raw)), 300))
	}

	var parsed proxyRuntimeSessionResponse
	if err := json.Unmarshal(raw, &parsed); err != nil {
		return nil, fmt.Errorf("parse proxy-runtime session/new response: %w", err)
	}
	out := map[string]any{
		"_proxy_runtime_session_started_at": time.Now().Unix(),
		"_proxy_runtime_pool_endpoints":     len(parsed.Pool.Endpoints),
		"_proxy_runtime_session_rotated":    true,
	}
	if parsed.Session.SessionID != "" {
		out["_proxy_runtime_session_hash"] = shortHash(parsed.Session.SessionID)
	}
	return out, nil
}
