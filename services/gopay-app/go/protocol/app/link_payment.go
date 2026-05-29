package app

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/byte-v-forge/gpt/gopay/protocol"
)

const (
	DefaultCurrency                   = "IDR"
	defaultServiceID                  = "1001"
	midtransMerchantTransferServiceID = "1002"
	gopayGatewayBaseURL               = "https://gwa.gopayapi.com"
)

type LinkPaymentOptions struct {
	PaymentLink    string
	PIN            string
	AmountValue    int64
	AmountCurrency string
	BodyLimit      int
}

type StepResult struct {
	Label        string
	StatusCode   int
	ResponseText string
	ErrorMessage string
}

type LinkPaymentResult struct {
	Success      bool
	ErrorMessage string
	PaymentID    string
	Status       string
	Steps        []StepResult
}

func RunLinkPayment(ctx context.Context, client *Client, options LinkPaymentOptions) (LinkPaymentResult, error) {
	if client == nil {
		err := fmt.Errorf("gopay app client is nil")
		return LinkPaymentResult{ErrorMessage: err.Error()}, err
	}
	if strings.TrimSpace(options.PIN) == "" {
		err := fmt.Errorf("pin is required")
		return LinkPaymentResult{ErrorMessage: err.Error()}, err
	}
	paymentRef, err := ExtractMidtransPaymentRef(options.PaymentLink)
	if err != nil {
		return LinkPaymentResult{ErrorMessage: err.Error()}, err
	}
	recorder := stepRecorder{limit: normalizeBodyLimit(options.BodyLimit)}
	status, err := RunGatewayPayment(ctx, client, paymentRef, options.PIN, &recorder)
	if err != nil {
		return recorder.result(paymentRef, "", err), err
	}
	return LinkPaymentResult{Success: true, PaymentID: paymentRef, Status: status, Steps: recorder.steps}, nil
}

func RunGatewayPayment(ctx context.Context, client *Client, paymentRef string, pin string, recorder *stepRecorder) (string, error) {
	if err := ValidateGatewayPayment(ctx, client, paymentRef, recorder); err != nil {
		return "", err
	}
	challengeID, clientID, err := ConfirmGatewayPayment(ctx, client, paymentRef, recorder)
	if err != nil {
		return "", err
	}
	pinToken, err := TokenizeNBPIN(ctx, client, pin, challengeID, clientID, recorder)
	if err != nil {
		return "", err
	}
	return ProcessGatewayPayment(ctx, client, paymentRef, pinToken, recorder)
}

func ValidateGatewayPayment(ctx context.Context, client *Client, paymentRef string, recorder *stepRecorder) error {
	endpoint := gopayGatewayBaseURL + "/v1/payment/validate?reference_id=" + url.QueryEscape(paymentRef)
	var lastErr error
	for attempt := 1; attempt <= 8; attempt++ {
		label := fmt.Sprintf("payment_validate_%d", attempt)
		resp, err := recorder.call(label, func() (*protocol.Response, error) {
			return client.Get(ctx, endpoint, http.StatusOK)
		})
		if err == nil && responseSuccess(resp) {
			return nil
		}
		if err != nil {
			lastErr = err
		} else {
			lastErr = fmt.Errorf("payment/validate not ready: %s", responseError(resp))
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(1500 * time.Millisecond):
		}
	}
	if lastErr == nil {
		lastErr = fmt.Errorf("payment/validate failed")
	}
	return lastErr
}

func ConfirmGatewayPayment(ctx context.Context, client *Client, paymentRef string, recorder *stepRecorder) (string, string, error) {
	endpoint := gopayGatewayBaseURL + "/v1/payment/confirm?reference_id=" + url.QueryEscape(paymentRef)
	resp, err := recorder.call("payment_confirm", func() (*protocol.Response, error) {
		return client.Post(ctx, endpoint, map[string]any{"payment_instructions": []any{}}, http.StatusOK)
	})
	if err != nil {
		return "", "", err
	}
	if !responseSuccess(resp) {
		return "", "", fmt.Errorf("payment/confirm failed: %s", responseError(resp))
	}
	return ExtractChallenge(resp)
}

func TokenizeNBPIN(ctx context.Context, client *Client, pin string, challengeID string, clientID string, recorder *stepRecorder) (string, error) {
	resp, err := recorder.call("pin_tokens_nb", func() (*protocol.Response, error) {
		return client.Post(ctx, CustomerBaseURL+"/api/v1/users/pin/tokens/nb", map[string]any{
			"challenge_id": challengeID,
			"client_id":    clientID,
			"pin":          pin,
		}, http.StatusOK)
	})
	if err == nil {
		if token, tokenErr := ExtractPinToken(resp); tokenErr == nil {
			return token, nil
		} else {
			err = tokenErr
		}
	}
	webResp, webErr := recorder.call("pin_tokens_nb_web", func() (*protocol.Response, error) {
		return client.TokenizePINWeb(ctx, pin, challengeID, clientID, http.StatusOK)
	})
	if webErr != nil {
		return "", fmt.Errorf("pin token failed: app=%v; web=%w", err, webErr)
	}
	token, tokenErr := ExtractPinToken(webResp)
	if tokenErr != nil {
		return "", fmt.Errorf("pin token missing: app=%v; web=%w", err, tokenErr)
	}
	return token, nil
}

func ProcessGatewayPayment(ctx context.Context, client *Client, paymentRef string, pinToken string, recorder *stepRecorder) (string, error) {
	endpoint := gopayGatewayBaseURL + "/v1/payment/process?reference_id=" + url.QueryEscape(paymentRef)
	resp, err := recorder.call("payment_process", func() (*protocol.Response, error) {
		return client.Post(ctx, endpoint, map[string]any{
			"challenge": map[string]any{
				"type":  "GOPAY_PIN_CHALLENGE",
				"value": map[string]any{"pin_token": pinToken},
			},
		}, http.StatusOK)
	})
	if err != nil {
		return "", err
	}
	if !responseSuccess(resp) || protocol.StringAt(resp.Data(), "next_action") != "payment-success" {
		return "", fmt.Errorf("payment/process failed: %s", responseError(resp))
	}
	return "PAID", nil
}

func RunPaymentOrder(ctx context.Context, client *Client, order map[string]any, pin string, recorder stepRecorder) (LinkPaymentResult, error) {
	paymentID := strings.TrimSpace(protocol.StringAt(order, "payment_id"))
	if paymentID == "" {
		err := fmt.Errorf("payment_id is required")
		return recorder.result("", "", err), err
	}
	checkout, err := recorder.call("checkout_list", func() (*protocol.Response, error) {
		return client.Post(ctx, CustomerBaseURL+"/v2/customer/payment-options/checkout/list", BuildCheckoutBody(order, midtransMerchantTransferServiceID), http.StatusOK)
	})
	if err != nil {
		return recorder.result(paymentID, "", err), err
	}
	paymentToken, err := ExtractPaymentToken(checkout)
	if err != nil {
		return recorder.result(paymentID, "", err), err
	}
	_, err = recorder.call("promotions_evaluate", func() (*protocol.Response, error) {
		return client.Post(ctx, CustomerBaseURL+"/v1/promotions/evaluate", BuildPromotionsEvaluateBody(order, paymentToken), http.StatusOK)
	})
	if err != nil {
		return recorder.result(paymentID, "", err), err
	}
	capturePaymentToken, err := RandomizePaymentOptionToken(paymentToken)
	if err != nil {
		return recorder.result(paymentID, "", err), err
	}
	capture1, err := recorder.call("capture1", func() (*protocol.Response, error) {
		headers := http.Header{"Idempotency-Key": []string{newTimeUUIDString()}}
		return client.Request(ctx, http.MethodPatch, CustomerBaseURL+"/v3/payments/"+paymentID+"/capture", BuildCaptureBody(order, capturePaymentToken, "", "", ""), headers, http.StatusOK)
	})
	if err != nil {
		return recorder.result(paymentID, "", err), err
	}
	challengeID, clientID, err := ExtractChallenge(capture1)
	if err != nil {
		return recorder.result(paymentID, "", err), err
	}
	_, err = recorder.call("last_used", func() (*protocol.Response, error) {
		return client.Put(ctx, CustomerBaseURL+"/v1/customer/payment-options/settings/last-used", map[string]any{"token": paymentToken}, http.StatusOK)
	})
	if err != nil {
		return recorder.result(paymentID, "", err), err
	}
	_, _ = recorder.call("pin_page", func() (*protocol.Response, error) {
		return client.Get(ctx, CustomerBaseURL+"/api/v2/challenges/"+challengeID+"/pin-page")
	})
	pinResp, err := recorder.call("pin_tokens", func() (*protocol.Response, error) {
		return client.Post(ctx, CustomerBaseURL+"/api/v1/users/pin/tokens", map[string]any{
			"pin":          pin,
			"client_id":    clientID,
			"challenge_id": challengeID,
		}, http.StatusOK)
	})
	if err != nil {
		return recorder.result(paymentID, "", err), err
	}
	pinToken, err := ExtractPinToken(pinResp)
	if err != nil {
		return recorder.result(paymentID, "", err), err
	}
	capture2, err := recorder.call("capture2", func() (*protocol.Response, error) {
		headers := http.Header{"Idempotency-Key": []string{newTimeUUIDString()}}
		return client.Request(ctx, http.MethodPatch, CustomerBaseURL+"/v3/payments/"+paymentID+"/capture", BuildCaptureBody(order, capturePaymentToken, pinToken, challengeID, clientID), headers, http.StatusOK)
	})
	if err != nil {
		return recorder.result(paymentID, "", err), err
	}
	status := strings.ToUpper(protocol.StringAt(capture2.Data(), "status"))
	if status != "PAID" {
		err := fmt.Errorf("payment not paid: status=%s", status)
		return recorder.result(paymentID, status, err), err
	}
	return LinkPaymentResult{Success: true, PaymentID: paymentID, Status: status, Steps: recorder.steps}, nil
}

var midtransPaymentRefRE = regexp.MustCompile(`A[0-9]{12,}[A-Za-z0-9]+ID`)

func ExtractMidtransPaymentRef(paymentLink string) (string, error) {
	match := midtransPaymentRefRE.FindString(strings.TrimSpace(paymentLink))
	if match == "" {
		return "", fmt.Errorf("midtrans payment reference missing from payment_link")
	}
	return match, nil
}

func BuildCheckoutBody(order map[string]any, serviceID string) map[string]any {
	merchantID := OrderMerchantID(order)
	return map[string]any{
		"intent": firstNonEmpty(protocol.StringAt(order, "payment_intent"), protocol.StringAt(order, "intent"), "EWALLET_QR"),
		"order_pricing": map[string]any{
			"payment_method_specific_pricing": []any{},
			"default_amount": map[string]any{
				"amount": OrderAmount(order),
			},
		},
		"selected_options_tokens": []any{},
		"merchant_id":             merchantID,
		"frontend_overrides": map[string]any{
			"offline_methods":        []any{},
			"payment_method_rollout": []any{},
			"exclude_paylater":       false,
		},
		"service_id": firstNonEmpty(serviceID, OrderServiceID(order), defaultServiceID),
		"metadata":   map[string]any{"merchant_id": merchantID},
	}
}

func BuildPromotionsEvaluateBody(order map[string]any, paymentToken string) map[string]any {
	return map[string]any{
		"order_id": protocol.StringAt(order, "payment_id"),
		"payment_instructions": []any{map[string]any{
			"token": paymentToken,
			"amount": map[string]any{
				"value":    OrderAmount(order),
				"currency": OrderCurrency(order),
			},
		}},
		"transaction_type": "MERCHANT_TRANSACTION",
	}
}

func BuildCaptureBody(order map[string]any, paymentToken string, pinToken string, challengeID string, clientID string) map[string]any {
	var challenge any
	if pinToken != "" {
		challenge = map[string]any{
			"action": nil,
			"value":  map[string]any{"pin_token": pinToken},
			"type":   "GOPAY_PIN_CHALLENGE",
			"metadata": map[string]any{
				"challenge_id": challengeID,
				"client_id":    clientID,
			},
		}
	}
	return map[string]any{
		"payment_instructions": []any{map[string]any{
			"token":           paymentToken,
			"amount":          map[string]any{"value": OrderAmount(order), "currency": OrderCurrency(order)},
			"admin_fee_token": nil,
		}},
		"applied_promo_code": []string{"NO_PROMO_APPLIED"},
		"description":        nil,
		"payment_method":     nil,
		"channel_type":       nil,
		"additional_data":    nil,
		"challenge":          challenge,
		"metadata":           nil,
		"checksum":           nil,
		"order_signature":    nil,
	}
}

func ExtractPaymentOrder(response *protocol.Response) (map[string]any, error) {
	if response == nil {
		return nil, fmt.Errorf("payment detail response is nil")
	}
	data := response.Data()
	if len(data) == 0 {
		return nil, fmt.Errorf("payment detail missing order data")
	}
	return map[string]any(data), nil
}

func ExtractPaymentToken(response *protocol.Response) (string, error) {
	for _, item := range paymentOptionItems(response) {
		if token := strings.TrimSpace(protocol.StringAt(item, "token")); token != "" {
			return token, nil
		}
	}
	return "", fmt.Errorf("payment option token missing")
}

func RandomizePaymentOptionToken(token string) (string, error) {
	payload, err := DecodePaymentOptionToken(token)
	if err != nil {
		return "", err
	}
	payload["payment_option_id"] = uuid.NewString()
	raw, err := protocol.CompactJSON(payload)
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(raw), nil
}

func DecodePaymentOptionToken(token string) (map[string]any, error) {
	token = strings.TrimSpace(token)
	if token == "" {
		return nil, fmt.Errorf("payment option token missing")
	}
	raw, err := base64.RawURLEncoding.DecodeString(token)
	if err != nil {
		raw, err = base64.StdEncoding.DecodeString(token)
	}
	if err != nil {
		return nil, fmt.Errorf("payment option token is not decodable JSON: %w", err)
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, fmt.Errorf("payment option token is not JSON: %w", err)
	}
	if strings.TrimSpace(protocol.StringAt(payload, "payment_option_id")) == "" {
		return nil, fmt.Errorf("payment option token missing payment_option_id")
	}
	return payload, nil
}

func ExtractChallenge(response *protocol.Response) (string, string, error) {
	data := response.Data()
	challengeID := firstNonEmpty(
		protocol.StringAt(data, "challenge", "action", "value", "challenge_id"),
		protocol.StringAt(data, "challenge", "value", "challenge_id"),
		protocol.StringAt(data, "challenge_id"),
		stringAtAnyKey(data, "challenge_id", "challengeId"),
	)
	clientID := firstNonEmpty(
		protocol.StringAt(data, "challenge", "action", "value", "client_id"),
		protocol.StringAt(data, "challenge", "value", "client_id"),
		protocol.StringAt(data, "client_id"),
		stringAtAnyKey(data, "client_id", "clientId"),
	)
	if challengeID == "" || clientID == "" {
		return "", "", fmt.Errorf("capture challenge missing")
	}
	return challengeID, clientID, nil
}

func ExtractPinToken(response *protocol.Response) (string, error) {
	data := response.Data()
	token := firstNonEmpty(protocol.StringAt(data, "token"), stringAtAnyKey(data, "pin_token", "pinToken", "token"))
	if token == "" {
		return "", fmt.Errorf("pin token missing")
	}
	return token, nil
}

func responseSuccess(response *protocol.Response) bool {
	if response == nil {
		return false
	}
	value := response.Payload["success"]
	if value == nil {
		value = response.Data()["success"]
	}
	switch typed := value.(type) {
	case bool:
		return typed
	case string:
		return strings.EqualFold(strings.TrimSpace(typed), "true")
	default:
		return false
	}
}

func responseError(response *protocol.Response) string {
	if response == nil {
		return "empty response"
	}
	for _, value := range []string{
		protocol.StringAt(response.Payload, "error_message"),
		protocol.StringAt(response.Payload, "message"),
		protocol.StringAt(response.Data(), "error_message"),
		protocol.StringAt(response.Data(), "message"),
		strings.TrimSpace(string(response.Body)),
	} {
		if value != "" {
			return protocol.Snippet(protocol.RedactText(value), 500)
		}
	}
	return fmt.Sprintf("http_status=%d", response.StatusCode)
}

func stringAtAnyKey(value any, keys ...string) string {
	wanted := map[string]struct{}{}
	for _, key := range keys {
		wanted[normalizeJSONKey(key)] = struct{}{}
	}
	var walk func(any) string
	walk = func(current any) string {
		if obj, ok := current.(map[string]any); ok {
			for key, item := range obj {
				if _, ok := wanted[normalizeJSONKey(key)]; ok {
					if text := strings.TrimSpace(fmt.Sprint(item)); text != "" && text != "<nil>" {
						return text
					}
				}
			}
			for _, item := range obj {
				if text := walk(item); text != "" {
					return text
				}
			}
		}
		if items, ok := current.([]any); ok {
			for _, item := range items {
				if text := walk(item); text != "" {
					return text
				}
			}
		}
		return ""
	}
	return walk(value)
}

func normalizeJSONKey(value string) string {
	value = strings.ToLower(strings.TrimSpace(value))
	value = strings.ReplaceAll(value, "_", "")
	value = strings.ReplaceAll(value, "-", "")
	return value
}

func OrderAmount(order map[string]any) int64 {
	if value := protocol.IntAt(order, "amount", "value"); value > 0 {
		return value
	}
	return protocol.IntAt(order, "additional_data", "aspiqr_information_v2", "transaction_details", "amount", "value")
}

func OrderCurrency(order map[string]any) string {
	return firstNonEmpty(protocol.StringAt(order, "amount", "currency"), DefaultCurrency)
}

func OrderServiceID(order map[string]any) string {
	return firstNonEmpty(
		protocol.StringAt(order, "service_id"),
		protocol.StringAt(order, "payment_widget_metadata", "service_id"),
		protocol.StringAt(order, "metadata", "service_id"),
		defaultServiceID,
	)
}

func OrderMerchantID(order map[string]any) string {
	return firstNonEmpty(
		protocol.StringAt(order, "payment_widget_metadata", "merchant_id"),
		protocol.StringAt(order, "merchant_information", "merchant_id"),
		protocol.StringAt(order, "merchant_information", "id"),
		protocol.StringAt(order, "additional_data", "merchant_information", "merchant_id"),
		protocol.StringAt(order, "additional_data", "merchant_information", "id"),
		protocol.StringAt(order, "additional_data", "aspiqr_information", "merchant_id"),
	)
}

func paymentOptionItems(response *protocol.Response) []map[string]any {
	if response == nil {
		return nil
	}
	var out []map[string]any
	collectItems := func(value any) {
		items, ok := value.([]any)
		if !ok {
			return
		}
		for _, item := range items {
			if obj, ok := item.(map[string]any); ok {
				out = append(out, obj)
			}
		}
	}
	data := response.Data()
	collectItems(data["selected_options"])
	collectItems(data["payment_options"])
	return out
}

type stepRecorder struct {
	steps []StepResult
	limit int
}

func (r *stepRecorder) call(label string, fn func() (*protocol.Response, error)) (*protocol.Response, error) {
	resp, err := fn()
	r.steps = append(r.steps, stepResult(label, resp, err, r.limit))
	return resp, err
}

func (r stepRecorder) result(paymentID string, status string, err error) LinkPaymentResult {
	message := ""
	if err != nil {
		message = err.Error()
	}
	return LinkPaymentResult{
		Success:      err == nil,
		ErrorMessage: message,
		PaymentID:    paymentID,
		Status:       status,
		Steps:        r.steps,
	}
}

func stepResult(label string, response *protocol.Response, err error, limit int) StepResult {
	result := StepResult{Label: label}
	if response != nil {
		result.StatusCode = response.StatusCode
		result.ResponseText = protocol.Snippet(protocol.RedactText(string(response.Body)), limit)
	}
	if err != nil {
		result.ErrorMessage = protocol.RedactText(err.Error())
	}
	return result
}

func normalizeBodyLimit(limit int) int {
	if limit <= 0 {
		return 1200
	}
	return limit
}
