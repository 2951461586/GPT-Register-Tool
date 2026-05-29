package protocol

import (
	"bytes"
	"compress/gzip"
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	xproxy "golang.org/x/net/proxy"
)

type Logger func(context.Context, string, map[string]any)

type RetryPolicy struct {
	Attempts int
	Backoff  time.Duration
}

func (p RetryPolicy) normalized() RetryPolicy {
	if p.Attempts < 1 {
		p.Attempts = 1
	}
	if p.Backoff <= 0 {
		p.Backoff = time.Second
	}
	return p
}

type Client struct {
	baseURL        *url.URL
	httpClient     HTTPDoer
	defaultHeaders http.Header
	retry          RetryPolicy
	logger         Logger
}

type HTTPDoer interface {
	Do(*http.Request) (*http.Response, error)
}

type Option func(*Client) error

func NewClient(baseURL string, opts ...Option) (*Client, error) {
	client := &Client{
		httpClient:     &http.Client{Timeout: 30 * time.Second},
		defaultHeaders: make(http.Header),
		retry:          RetryPolicy{Attempts: 1, Backoff: time.Second},
	}
	if strings.TrimSpace(baseURL) != "" {
		parsed, err := url.Parse(strings.TrimRight(baseURL, "/"))
		if err != nil {
			return nil, err
		}
		if parsed.Scheme == "" || parsed.Host == "" {
			return nil, &ConfigError{Field: "base_url", Msg: "must be an absolute URL"}
		}
		client.baseURL = parsed
	}
	for _, opt := range opts {
		if err := opt(client); err != nil {
			return nil, err
		}
	}
	client.retry = client.retry.normalized()
	return client, nil
}

func WithHTTPClient(httpClient *http.Client) Option {
	return func(client *Client) error {
		if httpClient == nil {
			return &ConfigError{Field: "http_client", Msg: "is nil"}
		}
		client.httpClient = httpClient
		return nil
	}
}

func WithHTTPDoer(httpClient HTTPDoer) Option {
	return func(client *Client) error {
		if httpClient == nil {
			return &ConfigError{Field: "http_client", Msg: "is nil"}
		}
		client.httpClient = httpClient
		return nil
	}
}

func WithHeader(key, value string) Option {
	return func(client *Client) error {
		key = strings.TrimSpace(key)
		if key == "" {
			return &ConfigError{Field: "header", Msg: "key is empty"}
		}
		deleteHeader(client.defaultHeaders, key)
		client.defaultHeaders[key] = []string{value}
		return nil
	}
}

func WithRetry(policy RetryPolicy) Option {
	return func(client *Client) error {
		client.retry = policy.normalized()
		return nil
	}
}

func WithLogger(logger Logger) Option {
	return func(client *Client) error {
		client.logger = logger
		return nil
	}
}

func NewHTTPClient(timeout time.Duration, proxyRawURL string) (*http.Client, error) {
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	proxyRawURL = strings.TrimSpace(proxyRawURL)
	if proxyRawURL != "" {
		parsed, err := url.Parse(proxyRawURL)
		if err != nil {
			return nil, err
		}
		switch parsed.Scheme {
		case "http", "https":
			transport.Proxy = http.ProxyURL(parsed)
		case "socks5", "socks5h":
			dialer, err := xproxy.SOCKS5("tcp", parsed.Host, nil, xproxy.Direct)
			if err != nil {
				return nil, err
			}
			transport.DialContext = func(ctx context.Context, network, address string) (net.Conn, error) {
				return dialer.Dial(network, address)
			}
		default:
			return nil, &ConfigError{Field: "proxy_url", Msg: "only http, https, socks5, and socks5h proxy URLs are supported by the Go protocol client"}
		}
	}
	return &http.Client{Timeout: timeout, Transport: transport}, nil
}

type Request struct {
	Method       string
	Path         string
	Query        url.Values
	Body         []byte
	Headers      http.Header
	Operation    string
	ExpectStatus []int
}

type Response struct {
	StatusCode int
	Header     http.Header
	Body       []byte
	Payload    JSONMap
}

func (r *Response) Data() JSONMap {
	return DataObject(r.Payload)
}

func (c *Client) Do(ctx context.Context, request Request) (*Response, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	method := strings.ToUpper(strings.TrimSpace(request.Method))
	if method == "" {
		method = http.MethodGet
	}
	target, err := c.requestURL(request.Path, request.Query)
	if err != nil {
		return nil, err
	}
	expected := statusSet(request.ExpectStatus)
	policy := c.retry.normalized()
	var lastErr error
	for attempt := 1; attempt <= policy.Attempts; attempt++ {
		resp, err := c.doOnce(ctx, method, target, request)
		if err == nil {
			if len(expected) == 0 || expected[resp.StatusCode] {
				return resp, nil
			}
			return resp, &HTTPError{
				Operation:  request.Operation,
				Method:     method,
				URL:        target.String(),
				StatusCode: resp.StatusCode,
				Body:       Snippet(RedactText(string(resp.Body)), 600),
			}
		}
		lastErr = err
		if attempt >= policy.Attempts || !retryableTransportError(err) {
			break
		}
		if c.logger != nil {
			c.logger(ctx, "gopay protocol retry", map[string]any{
				"operation": request.Operation,
				"host":      target.Host,
				"attempt":   attempt,
				"error":     RedactText(err.Error()),
			})
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(policy.Backoff * time.Duration(attempt)):
		}
	}
	return nil, lastErr
}

func (c *Client) doOnce(ctx context.Context, method string, target *url.URL, request Request) (*Response, error) {
	var body io.Reader
	if len(request.Body) > 0 {
		body = bytes.NewReader(request.Body)
	}
	httpReq, err := http.NewRequestWithContext(ctx, method, target.String(), body)
	if err != nil {
		return nil, err
	}
	copyHeadersExact(httpReq.Header, c.defaultHeaders)
	copyHeadersExact(httpReq.Header, request.Headers)
	if host := takeHostHeader(httpReq.Header); host != "" {
		httpReq.Host = host
	}
	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, readErr := readResponseBody(resp.Body)
	if readErr != nil {
		return nil, readErr
	}
	payload, _ := DecodeJSONMap(raw)
	return &Response{
		StatusCode: resp.StatusCode,
		Header:     resp.Header.Clone(),
		Body:       raw,
		Payload:    payload,
	}, nil
}

func copyHeadersExact(dst, src http.Header) {
	for key, values := range src {
		deleteHeader(dst, key)
		dst[key] = append([]string(nil), values...)
	}
}

func deleteHeader(headers http.Header, key string) {
	for existing := range headers {
		if strings.EqualFold(existing, key) {
			delete(headers, existing)
		}
	}
}

func takeHostHeader(headers http.Header) string {
	for key, values := range headers {
		if !strings.EqualFold(key, "Host") {
			continue
		}
		delete(headers, key)
		for _, value := range values {
			if value = strings.TrimSpace(value); value != "" {
				return value
			}
		}
		return ""
	}
	return ""
}

func readResponseBody(body io.Reader) ([]byte, error) {
	raw, err := io.ReadAll(io.LimitReader(body, 8*1024*1024))
	if err != nil {
		return nil, err
	}
	if bytes.HasPrefix(raw, []byte{0x1f, 0x8b}) {
		gzipReader, gzipErr := gzip.NewReader(bytes.NewReader(raw))
		if gzipErr != nil {
			return nil, gzipErr
		}
		defer gzipReader.Close()
		return io.ReadAll(io.LimitReader(gzipReader, 8*1024*1024))
	}
	return raw, nil
}

func (c *Client) requestURL(path string, query url.Values) (*url.URL, error) {
	path = strings.TrimSpace(path)
	if path == "" {
		return nil, &ConfigError{Field: "path", Msg: "is empty"}
	}
	parsed, err := url.Parse(path)
	if err != nil {
		return nil, err
	}
	if parsed.IsAbs() {
		if len(query) > 0 {
			values := parsed.Query()
			for key, items := range query {
				for _, item := range items {
					values.Add(key, item)
				}
			}
			parsed.RawQuery = values.Encode()
		}
		return parsed, nil
	}
	if c.baseURL == nil {
		return nil, &ConfigError{Field: "base_url", Msg: "is required for relative paths"}
	}
	out := *c.baseURL
	out.Path = strings.TrimRight(out.Path, "/") + "/" + strings.TrimLeft(path, "/")
	if len(query) > 0 {
		out.RawQuery = query.Encode()
	}
	return &out, nil
}

func statusSet(values []int) map[int]bool {
	out := make(map[int]bool, len(values))
	for _, value := range values {
		out[value] = true
	}
	return out
}

func retryableTransportError(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return false
	}
	text := strings.ToLower(err.Error())
	for _, hint := range []string{
		"tls",
		"connection reset",
		"connection refused",
		"timeout",
		"temporarily unavailable",
		"network is unreachable",
		"proxyconnect",
		"eof",
	} {
		if strings.Contains(text, hint) {
			return true
		}
	}
	return false
}
