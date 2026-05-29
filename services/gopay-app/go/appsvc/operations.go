package appsvc

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/byte-v-forge/gpt/gopay/protocol"
	gopayapp "github.com/byte-v-forge/gpt/gopay/protocol/app"
)

func (s *Server) getQrID(ctx context.Context, state stateMap) map[string]any {
	if stateString(state, "token") == "" {
		return map[string]any{"success": false, "error": "access_token missing"}
	}
	client, err := s.clientForState(ctx, state)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	resp, err := client.Get(ctx, customerBaseURL+"/v1/users/profile")
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if resp.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("users/profile failed", resp)}
	}
	qrID := stringForAnyKey(resp.Data(), "qr_id", "qrId")
	if qrID == "" {
		return map[string]any{"success": false, "error": "qr_id not found in response: " + compactErrorDetail(resp.Payload)}
	}
	return map[string]any{"success": true, "qr_id": qrID}
}

func (s *Server) unlink(ctx context.Context, state stateMap) map[string]any {
	client, err := s.clientForState(ctx, state)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	list, err := client.Get(ctx, customerBaseURL+"/v1/linkedapps")
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	if list.StatusCode != http.StatusOK {
		return map[string]any{"success": false, "error": apiError("linkedapps failed", list)}
	}
	services := linkedServices(list.Payload)
	unlinked := 0
	var failed []string
	for _, service := range services {
		path := protocol.StringAt(service, "unlink_service_url")
		if path == "" {
			continue
		}
		name := firstNonEmpty(protocol.StringAt(service, "service_name"), protocol.StringAt(service, "name"), path)
		resp, err := client.Patch(ctx, customerBaseURL+path, nil)
		if err == nil && (resp.StatusCode == http.StatusOK || resp.StatusCode == http.StatusAccepted || resp.StatusCode == http.StatusNoContent) {
			unlinked++
			continue
		}
		failed = append(failed, name)
	}
	if len(failed) > 0 {
		return map[string]any{"success": false, "error": "unlink failed: " + strings.Join(failed, ", "), "unlinked_count": unlinked}
	}
	state["stage"] = "consumed"
	delete(state, "last_error")
	return map[string]any{"success": true, "unlinked_count": unlinked}
}

func linkedServices(payload map[string]any) []map[string]any {
	var out []map[string]any
	var walk func(any)
	walk = func(value any) {
		switch typed := value.(type) {
		case map[string]any:
			if protocol.StringAt(typed, "unlink_service_url") != "" {
				out = append(out, typed)
			}
			for _, child := range typed {
				walk(child)
			}
		case []any:
			for _, child := range typed {
				walk(child)
			}
		}
	}
	walk(payload)
	return out
}

func (s *Server) claimEnvelope(ctx context.Context, state stateMap, envelopeID, link string) map[string]any {
	resolved, err := resolveEnvelopeRequestID(envelopeID, link)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	client, err := s.clientForState(ctx, state)
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	resp, err := client.Post(ctx, customerBaseURL+"/v1/festivals/envelope-requests", map[string]any{"envelope_request_id": resolved})
	if err != nil {
		return map[string]any{"success": false, "error": err.Error()}
	}
	raw := any(resp.Payload)
	data := resp.Data()
	if resp.StatusCode == http.StatusOK {
		detail, err := client.Get(ctx, customerBaseURL+"/v1/festivals/envelope-requests/"+url.PathEscape(resolved))
		if err == nil && detail.StatusCode == http.StatusOK {
			raw = map[string]any{"claim": resp.Payload, "detail": detail.Payload}
			data = detail.Data()
		}
	}
	success := resp.StatusCode == http.StatusOK
	errMessage := ""
	if !success {
		errMessage = apiError("claim envelope failed", resp)
	}
	return map[string]any{
		"success":                      success,
		"error":                        errMessage,
		"envelope_request_id":          resolved,
		"response_envelope_request_id": protocol.StringAt(data, "envelope_request_id"),
		"status":                       protocol.StringAt(data, "status"),
		"http_status":                  resp.StatusCode,
		"raw_json":                     safeJSON(raw),
	}
}

func (s *Server) replayLinkPayment(ctx context.Context, state stateMap, paymentLink, pin string, amountValue int64, amountCurrency string, bodyLimit int) (gopayapp.LinkPaymentResult, error) {
	client, err := s.clientForState(ctx, state)
	if err != nil {
		return gopayapp.LinkPaymentResult{}, err
	}
	return gopayapp.RunLinkPayment(ctx, client, gopayapp.LinkPaymentOptions{
		PaymentLink:    paymentLink,
		PIN:            pin,
		AmountValue:    amountValue,
		AmountCurrency: firstNonEmpty(amountCurrency, "IDR"),
		BodyLimit:      bodyLimit,
	})
}

var envelopeIDRE = regexp.MustCompile(`[A-Za-z0-9_-]{8,128}`)

func resolveEnvelopeRequestID(envelopeID, link string) (string, error) {
	for _, source := range []string{envelopeID, link} {
		for _, candidate := range decodedCandidates(source) {
			if match := regexp.MustCompile(`/v1/festivals/envelope-requests/([^/?#'" <]+)`).FindStringSubmatch(candidate); len(match) > 1 {
				return match[1], nil
			}
			parsed, _ := url.Parse(candidate)
			if parsed != nil {
				query := parsed.Query()
				for _, key := range []string{"envelope_request_id", "envelopeRequestId", "id", "data", "link", "deep_link_value", "af_dp"} {
					for _, item := range query[key] {
						if resolved, err := resolveEnvelopeRequestID(item, ""); err == nil {
							return resolved, nil
						}
					}
				}
			}
			if envelopeIDRE.MatchString(candidate) && envelopeIDRE.FindString(candidate) == candidate {
				return candidate, nil
			}
		}
	}
	return "", fmt.Errorf("envelope_request_id required, or pass a link containing one")
}

func decodedCandidates(value string) []string {
	current := strings.TrimSpace(value)
	var out []string
	seen := map[string]bool{}
	for range 5 {
		if current != "" && !seen[current] {
			out = append(out, current)
			seen[current] = true
		}
		decoded, err := url.QueryUnescape(current)
		if err != nil || decoded == current {
			break
		}
		current = decoded
	}
	return out
}

func unixNow() int64 {
	return time.Now().Unix()
}
