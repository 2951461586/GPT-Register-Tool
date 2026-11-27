using SmsWorkbench.Localization;

namespace SmsWorkbench.Services
{
    /// <summary>
    /// Window-independent runner for the non-CAPTCHA Stripe Hosted (<c>cs_*</c>)
    /// and ChatGPT Custom (<c>oaics_*</c>) protocol card quote/payment commands.
    /// <para>
    /// Raw card fields are serialized strictly into the child-process
    /// <c>GPT_CARD_JSON</c> environment variable via
    /// <see cref="BackendCommandPlanner.CreateProtocolCardPayment"/> and never
    /// enter CLI arguments, disk files, or logs.
    /// </para>
    /// </summary>
    public interface IProtocolCardPaymentService
    {
        Task<ProtocolCardPaymentResult> ExecuteAsync(
            ProtocolCardPaymentRequest request,
            CancellationToken cancellationToken);

        string ResolveDefaultProxy();
    }

    public sealed class ProtocolCardPaymentService : IProtocolCardPaymentService
    {
        private readonly IBackendTaskCoordinator _backendTasks;
        private readonly ISettingsService _settings;

        public ProtocolCardPaymentService(
            IBackendTaskCoordinator backendTasks,
            ISettingsService settings)
        {
            _backendTasks = backendTasks;
            _settings = settings;
        }

        public string ResolveDefaultProxy()
        {
            foreach (string listPath in new[] { "protocol_payments.proxy_pool", "proxy.lanes.payment.pool" })
            {
                string first = _settings.GetStringList(listPath)
                    .Select(item => ProxyInputNormalizer.Normalize(item))
                    .FirstOrDefault(item => item.Length > 0) ?? "";
                if (first.Length > 0) return first;
            }

            foreach (string scalarPath in new[] { "proxy.default", "proxy.registration" })
            {
                string normalized = ProxyInputNormalizer.Normalize(_settings.GetString(scalarPath));
                if (normalized.Length > 0) return normalized;
            }

            return "";
        }

        public async Task<ProtocolCardPaymentResult> ExecuteAsync(
            ProtocolCardPaymentRequest request,
            CancellationToken cancellationToken)
        {
            ArgumentNullException.ThrowIfNull(request);

            string? cardJson = BuildCardJson(request);
            string normalizedProxy = ProxyInputNormalizer.Normalize(request.Proxy);
            BackendCommandPlan plan = BackendCommandPlanner.CreateProtocolCardPayment(
                checkoutUrl: request.CheckoutUrl,
                publishableKey: request.PublishableKey,
                quoteOnly: request.QuoteOnly,
                billingCountry: request.BillingCountry,
                paymentMethodId: request.PaymentMethodId,
                cardJson: cardJson,
                readOnly: request.ReadOnly,
                proxy: normalizedProxy,
                email: request.Email,
                sessionFile: request.SessionFile,
                timeoutSeconds: request.TimeoutSeconds);

            BackendCommandResult result = await _backendTasks.RunAsync(
                BackendCommand.Create(
                    plan.TaskName,
                    plan.Arguments,
                    plan.TimeoutMilliseconds ?? 120_000,
                    plan.Environment),
                cancellationToken: cancellationToken).ConfigureAwait(false);

            if (result.TimedOut)
                throw new TimeoutException(Loc.T("协议卡付命令超时。"));
            if (result.Payload.HasValue)
                return ProtocolCardPaymentResult.Parse(result.Payload.Value, request.QuoteOnly, request.CheckoutUrl);
            if (!string.IsNullOrWhiteSpace(result.StandardError))
                throw new InvalidOperationException(SensitiveDataSanitizer.Redact(result.StandardError));
            throw new InvalidOperationException(Loc.T("后端未返回协议卡付结果。"));
        }

        private static string? BuildCardJson(ProtocolCardPaymentRequest request)
        {
            if (request.QuoteOnly || request.ReadOnly || !string.IsNullOrWhiteSpace(request.PaymentMethodId))
                return null;

            string number = DigitsOnly(request.CardNumber);
            string month = DigitsOnly(request.CardExpMonth);
            string year = DigitsOnly(request.CardExpYear);
            string cvc = DigitsOnly(request.CardCvc);
            if (number.Length == 0 && month.Length == 0 && year.Length == 0 && cvc.Length == 0)
                return null;

            return JsonSerializer.Serialize(new
            {
                number,
                exp_month = month,
                exp_year = year,
                cvc,
            });
        }

        private static string DigitsOnly(string? value)
        {
            if (string.IsNullOrWhiteSpace(value)) return "";
            var builder = new StringBuilder(value.Length);
            foreach (char ch in value)
            {
                if (ch is >= '0' and <= '9')
                    builder.Append(ch);
            }
            return builder.ToString();
        }
    }
}
