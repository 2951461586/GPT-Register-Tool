package gateway

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"
)

func TestLiveInspectStripeInit(t *testing.T) {
	if os.Getenv("PP_LIVE_INSPECT") != "1" {
		t.Skip("set PP_LIVE_INSPECT=1")
	}
	token, _ := os.ReadFile("/tmp/pp_at.txt")
	proxy, _ := os.ReadFile("/tmp/pp_proxy_url.txt")
	cfg := DefaultConfig()
	cfg.Country = "US"
	cfg.Currency = "USD"
	cfg.Timeout = 45 * time.Second
	e := NewExtractor(cfg)
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	hosted, checkoutPK, err := e.createCheckoutSingle(ctx, string(token), string(proxy))
	if err != nil {
		t.Fatal(err)
	}
	client, err := e.newHTTPClient(string(proxy), cfg.Timeout)
	if err != nil {
		t.Fatal(err)
	}
	defer client.CloseIdleConnections()
	pk, cs, init, err := e.fetchCheckoutInit(ctx, client, hosted, checkoutPK)
	if err != nil {
		t.Fatal(err)
	}
	_ = pk
	_ = cs
	keys := []string{
		"id", "object", "session_id", "ui_mode", "mode", "status", "payment_status",
		"currency", "return_url", "url", "stripe_hosted_url", "payment_method_types",
		"ordered_payment_method_types", "automatic_payment_method_types", "enabled_third_party_wallets",
		"payment_method_specs", "payment_method_options", "lpm_settings", "setup_intent",
		"payment_intent", "invoice", "total_summary", "line_item_group", "setup_future_usage",
		"setup_future_usage_for_payment_method_type", "payment_method_collection", "use_payment_methods",
		"redirect_on_completion", "origin_context", "elements_options", "permissions", "link_settings",
		"managed_payments", "feature_flags", "customer_email", "geocoding",
	}
	out := map[string]any{}
	for _, key := range keys {
		if init[key] != nil {
			out[key] = init[key]
		}
	}
	raw, _ := json.MarshalIndent(out, "", "  ")
	t.Logf("init=%s", sanitizeErrorSnippet(string(raw), 20000))
}
