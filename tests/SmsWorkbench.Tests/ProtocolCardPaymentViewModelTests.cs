using System;
using System.Collections.Generic;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using SmsWorkbench.Models;
using SmsWorkbench.Services;
using SmsWorkbench.ViewModels;
using Xunit;

namespace SmsWorkbench.Tests
{
    public sealed class ProtocolCardPaymentViewModelTests
    {
        [Fact]
        public void Parse_FormatsZeroDecimalAndTwoDecimalCurrencies()
        {
            using var jpyDoc = JsonDocument.Parse("""
                {
                  "ok": true,
                  "session_id": "cs_live_a1test",
                  "checkout_url": "https://checkout.example.test/c/pay/cs_live_a1test",
                  "amount_minor": 3000,
                  "currency": "jpy"
                }
                """);
            ProtocolCardPaymentResult jpy = ProtocolCardPaymentResult.Parse(jpyDoc.RootElement, quoteOnly: true);
            Assert.True(jpy.Ok);
            Assert.Equal("quoted", jpy.State);
            Assert.Equal("success", jpy.BadgeSeverity);
            Assert.Equal("3000 JPY", jpy.AmountDisplay);

            using var usdDoc = JsonDocument.Parse("""
                {
                  "ok": true,
                  "state": "paid",
                  "mode": "hosted_checkout",
                  "amount_minor": 2000,
                  "currency": "usd",
                  "intent_status": "succeeded"
                }
                """);
            ProtocolCardPaymentResult usd = ProtocolCardPaymentResult.Parse(usdDoc.RootElement, quoteOnly: false);
            Assert.True(usd.Ok);
            Assert.Equal("paid", usd.State);
            Assert.Equal("20.00 USD", usd.AmountDisplay);
            Assert.Equal("succeeded", usd.IntentStatus);

            // Stripe treats IDR as a 2-decimal currency (aligned with Python & Rust ZERO_DECIMAL_CURRENCIES).
            Assert.Equal("300000.00 IDR", ProtocolCardPaymentResult.FormatAmount(30_000_000, "idr"));

            using var unknownDoc = JsonDocument.Parse("""
                {
                  "ok": false,
                  "status": "unknown",
                  "state": "unknown",
                  "mode": "hosted_checkout",
                  "code": "payment_outcome_unknown",
                  "message": "Stripe confirm outcome unknown"
                }
                """);
            ProtocolCardPaymentResult unknown = ProtocolCardPaymentResult.Parse(unknownDoc.RootElement, quoteOnly: false);
            Assert.False(unknown.Ok);
            Assert.Equal("unknown", unknown.State);
            Assert.Equal("warn", unknown.BadgeSeverity);
            Assert.Equal(SmsWorkbench.Localization.Loc.T("结果未知（需核账）"), unknown.BadgeLabel);
        }

        [Fact]
        public async Task QuoteCommand_ValidatesInputsAndAppliesQuoteResult()
        {
            var stub = new StubProtocolCardPaymentService(req =>
            {
                using var doc = JsonDocument.Parse("""
                    {
                      "ok": true,
                      "session_id": "cs_test_quote",
                      "amount_minor": 0,
                      "currency": "usd"
                    }
                    """);
                return ProtocolCardPaymentResult.Parse(doc.RootElement, quoteOnly: req.QuoteOnly, req.CheckoutUrl);
            });

            var vm = new ProtocolCardPaymentViewModel(stub);
            await vm.QuoteCommand.ExecuteAsync(null);
            Assert.Empty(stub.Requests);
            Assert.Contains("HTTPS", vm.StatusText, StringComparison.Ordinal);

            vm.CheckoutUrl = "https://checkout.example.test/c/pay/cs_test_quote";
            vm.PublishableKey = "not_a_pk";
            await vm.QuoteCommand.ExecuteAsync(null);
            Assert.Empty(stub.Requests);
            Assert.Contains("pk_", vm.StatusText, StringComparison.Ordinal);

            vm.PublishableKey = "pk_test_123456";
            await vm.QuoteCommand.ExecuteAsync(null);

            ProtocolCardPaymentRequest recorded = Assert.Single(stub.Requests);
            Assert.True(recorded.QuoteOnly);
            Assert.Equal("US", recorded.BillingCountry);
            Assert.True(vm.HasResult);
            Assert.Equal("0.00 USD", vm.AmountSummary);
            Assert.Equal("success", vm.ResultBadgeSeverity);
            Assert.True(vm.HasResultJson);
        }

        [Fact]
        public async Task PayCommand_WipesSensitiveCardFieldsAfterRunAndSupportsManualChallengeHandoff()
        {
            string? openedUrl = null;
            string? copiedJson = null;

            var stub = new StubProtocolCardPaymentService(req =>
            {
                using var doc = JsonDocument.Parse("""
                    {
                      "ok": false,
                      "status": "challenge_required",
                      "state": "challenge_required",
                      "mode": "hosted_checkout",
                      "session_id": "cs_test_3ds",
                      "checkout_url": "https://checkout.example.test/c/pay/cs_test_3ds",
                      "challenge_type": "three_d_secure_2",
                      "manual_action_required": true,
                      "message": "3DS verification required"
                    }
                    """);
                return ProtocolCardPaymentResult.Parse(doc.RootElement, quoteOnly: req.QuoteOnly, req.CheckoutUrl);
            });

            var vm = new ProtocolCardPaymentViewModel(
                stub,
                initialCheckoutUrl: "https://checkout.example.test/c/pay/cs_test_3ds",
                initialAccountEmail: "alice@example.com",
                initialSessionFile: "C:\\sessions\\alice.json",
                openUrl: url => openedUrl = url,
                copyText: text => copiedJson = text)
            {
                PublishableKey = "pk_test_123456",
                CardNumber = "4242 4242 4242 4242",
                CardExpMonth = "12",
                CardExpYear = "2029",
                CardCvc = "123",
            };

            await vm.PayCommand.ExecuteAsync(null);

            ProtocolCardPaymentRequest recorded = Assert.Single(stub.Requests);
            Assert.False(recorded.QuoteOnly);
            Assert.Equal("alice@example.com", recorded.Email);
            Assert.Equal("C:\\sessions\\alice.json", recorded.SessionFile);

            // Card PAN and CVC must be cleared automatically once Pay completes.
            Assert.Equal("", vm.CardNumber);
            Assert.Equal("", vm.CardCvc);

            Assert.True(vm.ManualActionRequired);
            Assert.True(vm.HasChallengeUrl);
            Assert.Equal("warn", vm.ResultBadgeSeverity);

            Assert.True(vm.OpenCheckoutUrlCommand.CanExecute(null));
            vm.OpenCheckoutUrlCommand.Execute(null);
            Assert.Equal("https://checkout.example.test/c/pay/cs_test_3ds", openedUrl);

            Assert.True(vm.CopyResultJsonCommand.CanExecute(null));
            vm.CopyResultJsonCommand.Execute(null);
            Assert.NotNull(copiedJson);
            Assert.Contains("challenge_required", copiedJson, StringComparison.Ordinal);
        }

        [Fact]
        public async Task PayCommand_ReadOnlyModeSkipsCardValidation()
        {
            var stub = new StubProtocolCardPaymentService(req =>
            {
                using var doc = JsonDocument.Parse("""
                    {
                      "ok": true,
                      "status": "read_only",
                      "mode": "custom_checkout",
                      "amount_minor": 0,
                      "currency": "usd"
                    }
                    """);
                return ProtocolCardPaymentResult.Parse(doc.RootElement, quoteOnly: req.QuoteOnly, req.CheckoutUrl);
            });

            var vm = new ProtocolCardPaymentViewModel(stub)
            {
                CheckoutUrl = "https://checkout.example.test/checkout/oaics_test_123",
                PublishableKey = "pk_test_123456",
                ReadOnlyMode = true,
            };

            await vm.PayCommand.ExecuteAsync(null);
            ProtocolCardPaymentRequest recorded = Assert.Single(stub.Requests);
            Assert.True(recorded.ReadOnly);
            Assert.Equal("success", vm.ResultBadgeSeverity);
        }

        [Fact]
        public async Task PayCommand_RejectsExistingPaymentMethodIdForOaicsCustomCheckout()
        {
            var stub = new StubProtocolCardPaymentService(_ => throw new InvalidOperationException("should not call backend"));
            var vm = new ProtocolCardPaymentViewModel(stub)
            {
                CheckoutUrl = "https://chatgpt.com/checkout/openai_llc/oaics_test_999",
                PublishableKey = "pk_test_123456",
                PaymentMethodId = "pm_existing_123",
            };

            await vm.PayCommand.ExecuteAsync(null);
            Assert.Empty(stub.Requests);
            Assert.Contains("oaics_", vm.StatusText, StringComparison.OrdinalIgnoreCase);
        }

        [Fact]
        public async Task QuoteCommand_OaicsReadOnlyResponseDisplaysQuotedAmount()
        {
            var stub = new StubProtocolCardPaymentService(req =>
            {
                using var doc = JsonDocument.Parse("""
                    {
                      "ok": true,
                      "status": "read_only",
                      "state": "read_only",
                      "mode": "custom_checkout",
                      "session_id": "oaics_test_quote",
                      "amount_minor": 3000,
                      "currency": "JPY"
                    }
                    """);
                return ProtocolCardPaymentResult.Parse(doc.RootElement, quoteOnly: req.QuoteOnly, req.CheckoutUrl);
            });

            var vm = new ProtocolCardPaymentViewModel(stub)
            {
                CheckoutUrl = "https://chatgpt.com/checkout/openai_llc/oaics_test_quote",
                PublishableKey = "pk_test_123456",
            };

            await vm.QuoteCommand.ExecuteAsync(null);
            Assert.True(vm.HasResult);
            Assert.Equal("success", vm.ResultBadgeSeverity);
            Assert.Equal("3000 JPY", vm.AmountSummary);
        }

        [Fact]
        public async Task Service_PassesCardJsonOnlyViaEnvironmentNotArguments()
        {
            using var responseDoc = JsonDocument.Parse("""{"ok": true, "state": "paid", "amount_minor": 0, "currency": "usd"}""");
            var coordinator = new RecordingBackendTaskCoordinator(responseDoc.RootElement.Clone());
            var settings = new FakeSettingsService();
            var service = new ProtocolCardPaymentService(coordinator, settings);

            ProtocolCardPaymentResult result = await service.ExecuteAsync(
                new ProtocolCardPaymentRequest(
                    CheckoutUrl: "https://checkout.example.test/c/pay/cs_test_env",
                    PublishableKey: "pk_test_env",
                    BillingCountry: "JP",
                    Proxy: "proxy.example:8080:alice:secret",
                    PaymentMethodId: "",
                    CardNumber: "4242 4242 4242 4242",
                    CardExpMonth: "08",
                    CardExpYear: "2030",
                    CardCvc: "999",
                    ReadOnly: false,
                    Email: "alice@example.com",
                    SessionFile: "",
                    QuoteOnly: false,
                    TimeoutSeconds: 45),
                CancellationToken.None);

            Assert.True(result.Ok);
            BackendCommand captured = Assert.Single(coordinator.Commands);
            Assert.Contains("--protocol-card-pay", captured.Arguments);
            Assert.Contains("--desktop-ipc", captured.Arguments);
            Assert.Contains("--checkout-country", captured.Arguments);
            Assert.Contains("JP", captured.Arguments);
            Assert.Contains("--proxy", captured.Arguments);
            Assert.DoesNotContain(captured.Arguments, arg => arg.Contains("4242424242424242", StringComparison.Ordinal));
            Assert.DoesNotContain(captured.Arguments, arg => arg.Contains("999", StringComparison.Ordinal));
            Assert.True(captured.EnvironmentVariables.TryGetValue("GPT_CARD_JSON", out string? cardEnv));
            Assert.Contains("\"number\":\"4242424242424242\"", cardEnv, StringComparison.Ordinal);
            Assert.Contains("\"cvc\":\"999\"", cardEnv, StringComparison.Ordinal);
        }

        private sealed class StubProtocolCardPaymentService : IProtocolCardPaymentService
        {
            private readonly Func<ProtocolCardPaymentRequest, ProtocolCardPaymentResult> _handler;

            public List<ProtocolCardPaymentRequest> Requests { get; } = new();

            public StubProtocolCardPaymentService(Func<ProtocolCardPaymentRequest, ProtocolCardPaymentResult> handler)
            {
                _handler = handler;
            }

            public string ResolveDefaultProxy() => "";

            public Task<ProtocolCardPaymentResult> ExecuteAsync(
                ProtocolCardPaymentRequest request,
                CancellationToken cancellationToken)
            {
                Requests.Add(request);
                return Task.FromResult(_handler(request));
            }
        }

        private sealed class RecordingBackendTaskCoordinator : IBackendTaskCoordinator
        {
            private readonly JsonElement _payload;

            public List<BackendCommand> Commands { get; } = new();

            public RecordingBackendTaskCoordinator(JsonElement payload)
            {
                _payload = payload;
            }

            public bool IsRunning => false;

            public Task<BackendCommandResult> RunAsync(
                BackendCommand command,
                IProgress<BackendOutputLine>? progress = null,
                CancellationToken cancellationToken = default)
            {
                Commands.Add(command);
                return Task.FromResult(new BackendCommandResult(0, "", "", _payload, false));
            }

            public Task<string> RunForResultAsync(
                BackendCommand command,
                IProgress<BackendOutputLine>? progress = null,
                CancellationToken cancellationToken = default)
            {
                Commands.Add(command);
                return Task.FromResult(_payload.GetRawText());
            }

            public bool Cancel() => false;
        }

        private sealed class FakeSettingsService : ISettingsService
        {
            public string ConfigPath => "";
            public IReadOnlyList<SettingsCategoryViewModel> Load() => Array.Empty<SettingsCategoryViewModel>();
            public SettingsSaveResult Save(IEnumerable<SettingsCategoryViewModel> categories) => new(true);
            public string GetString(string path, string fallback = "") => fallback;
            public IReadOnlyList<string> GetStringList(string path) => Array.Empty<string>();
            public void UpdateConfig(Action<System.Text.Json.Nodes.JsonObject> mutate) { }
            public void ReloadProviderScopedFields(IEnumerable<SettingFieldViewModel> fields, string provider) { }
        }
    }
}
