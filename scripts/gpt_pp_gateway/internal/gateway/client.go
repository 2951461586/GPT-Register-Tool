package gateway

import (
	"io"
	stdhttp "net/http"
	"strings"
	"time"

	http "github.com/bogdanfinn/fhttp"
	tlsclient "github.com/bogdanfinn/tls-client"
	"github.com/bogdanfinn/tls-client/profiles"
)

type httpDoer interface {
	Do(req *http.Request) (*http.Response, error)
	CloseIdleConnections()
}

type tlsHTTPDoer interface {
	httpDoer
	SetProxy(proxyUrl string) error
	GetProxy() string
	GetCookieJar() http.CookieJar
}

func (e *Extractor) newHTTPClient(proxyURL string, timeout time.Duration) (httpDoer, error) {
	return e.newHTTPClientWithJar(proxyURL, timeout, tlsclient.NewCookieJar())
}

func (e *Extractor) newHTTPClientWithJar(proxyURL string, timeout time.Duration, jar http.CookieJar) (tlsHTTPDoer, error) {
	if timeout <= 0 {
		timeout = e.Config.Timeout
	}
	if jar == nil {
		jar = tlsclient.NewCookieJar()
	}
	options := []tlsclient.HttpClientOption{
		tlsclient.WithTimeoutSeconds(int(timeout.Seconds())),
		tlsclient.WithClientProfile(profiles.Chrome_133),
		tlsclient.WithCookieJar(jar),
		tlsclient.WithNotFollowRedirects(),
	}
	if proxyURL != "" {
		options = append(options, tlsclient.WithProxyUrl(proxyURL))
	}
	return tlsclient.NewHttpClient(tlsclient.NewNoopLogger(), options...)
}

type stdFHTTPAdapter struct {
	client *stdhttp.Client
}

func (a *stdFHTTPAdapter) Do(req *http.Request) (*http.Response, error) {
	var body io.Reader
	if req.Body != nil {
		body = req.Body
	}
	stdReq, err := stdhttp.NewRequestWithContext(req.Context(), req.Method, req.URL.String(), body)
	if err != nil {
		return nil, err
	}
	for key, values := range req.Header {
		if strings.HasSuffix(key, ":") {
			continue
		}
		for _, value := range values {
			stdReq.Header.Add(key, value)
		}
	}
	resp, err := a.client.Do(stdReq)
	if err != nil {
		return nil, err
	}
	out := &http.Response{
		Status:     resp.Status,
		StatusCode: resp.StatusCode,
		Header:     http.Header{},
		Body:       resp.Body,
	}
	for key, values := range resp.Header {
		for _, value := range values {
			out.Header.Add(key, value)
		}
	}
	return out, nil
}

func (a *stdFHTTPAdapter) CloseIdleConnections() {
	a.client.CloseIdleConnections()
}

func (e *Extractor) newHTTP1ClientAdapter(proxyURL string, timeout time.Duration) (httpDoer, error) {
	client, err := e.newStdHTTP1Client(proxyURL, timeout)
	if err != nil {
		return nil, err
	}
	return &stdFHTTPAdapter{client: client}, nil
}
