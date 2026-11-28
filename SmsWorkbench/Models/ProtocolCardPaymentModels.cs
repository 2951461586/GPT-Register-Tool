using SmsWorkbench.Localization;

namespace SmsWorkbench.Models
{
    public sealed record ProtocolCardCountryOption(string Code, string DisplayName);

    public sealed record ProtocolCardPaymentRequest(
        string CheckoutUrl,
        string PublishableKey,
        string BillingCountry,
        string Proxy,
        string PaymentMethodId,
        string CardNumber,
        string CardExpMonth,
        string CardExpYear,
        string CardCvc,
        bool ReadOnly,
        string Email,
        string SessionFile,
        bool QuoteOnly,
        int TimeoutSeconds = 60);

    public sealed record ProtocolCardPaymentResult(
        bool Ok,
        bool QuoteOnly,
        string State,
        string Mode,
        string SessionId,
        string CheckoutUrl,
        long? AmountMinor,
        string Currency,
        string AmountDisplay,
        string PaymentMethodId,
        string IntentStatus,
        string ChargeId,
        string ChallengeType,
        bool ManualActionRequired,
        string Code,
        string DeclineCode,
        string ErrorCode,
        string ErrorStage,
        string Message,
        string FailureDetail,
        string RawJson)
    {
        private static readonly JsonSerializerOptions IndentedJson = new() { WriteIndented = true };

        private static readonly HashSet<string> ZeroDecimalCurrencies = new(StringComparer.OrdinalIgnoreCase)
        {
            "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW",
            "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
        };

        public string BadgeSeverity => State switch
        {
            "paid" or "quoted" or "read_only" when Ok => "success",
            "challenge_required" or "pending" or "unknown" => "warn",
            "card_declined" or "confirm_error" or "failed" => "danger",
            _ => Ok ? "success" : "danger",
        };

        public string BadgeLabel => State switch
        {
            "quoted" => Loc.T("报价成功"),
            "paid" => Loc.T("支付成功"),
            "read_only" => Loc.T("只读核账完成"),
            "challenge_required" => Loc.T("需要人工验证"),
            "card_declined" => Loc.T("银行卡被拒"),
            "confirm_error" => Loc.T("确认失败"),
            "pending" => Loc.T("处理中"),
            "unknown" => Loc.T("结果未知（需核账）"),
            "failed" => Loc.T("执行失败"),
            _ => string.IsNullOrWhiteSpace(State) ? (Ok ? Loc.T("成功") : Loc.T("失败")) : State,
        };

        public string SummaryMessage
        {
            get
            {
                if (!string.IsNullOrWhiteSpace(Message)) return Message;
                if (!string.IsNullOrWhiteSpace(FailureDetail)) return FailureDetail;
                if (!string.IsNullOrWhiteSpace(DeclineCode)) return DeclineCode;
                if (!string.IsNullOrWhiteSpace(Code)) return Code;
                if (!string.IsNullOrWhiteSpace(ErrorCode)) return ErrorCode;
                if (ManualActionRequired)
                    return Loc.T("遇到 3DS / 人机挑战，请在浏览器打开结账页面手动完成验证。");
                return Ok ? BadgeLabel : Loc.T("协议卡付未完成，请查看返回详情。");
            }
        }

        public static ProtocolCardPaymentResult Parse(
            JsonElement payload,
            bool quoteOnly,
            string fallbackCheckoutUrl = "")
        {
            string rawJson = SensitiveDataSanitizer.Redact(JsonSerializer.Serialize(payload, IndentedJson));
            if (payload.ValueKind != JsonValueKind.Object)
            {
                return new ProtocolCardPaymentResult(
                    Ok: false,
                    QuoteOnly: quoteOnly,
                    State: "failed",
                    Mode: "",
                    SessionId: "",
                    CheckoutUrl: fallbackCheckoutUrl,
                    AmountMinor: null,
                    Currency: "",
                    AmountDisplay: "",
                    PaymentMethodId: "",
                    IntentStatus: "",
                    ChargeId: "",
                    ChallengeType: "",
                    ManualActionRequired: false,
                    Code: "",
                    DeclineCode: "",
                    ErrorCode: "invalid_payload",
                    ErrorStage: "",
                    Message: Loc.T("后端返回了无效的 JSON 对象。"),
                    FailureDetail: "",
                    RawJson: rawJson);
            }

            bool ok = ReadBool(payload, "ok");
            string rawState = ReadString(payload, "state").ToLowerInvariant();
            string status = ReadString(payload, "status").ToLowerInvariant();
            string mode = ReadString(payload, "mode");
            string sessionId = ReadString(payload, "session_id");
            string checkoutUrl = ReadString(payload, "checkout_url");
            if (checkoutUrl.Length == 0) checkoutUrl = fallbackCheckoutUrl;

            long? amountMinor = ReadNullableLong(payload, "amount_minor");
            string currency = ReadString(payload, "currency").ToUpperInvariant();
            string amountDisplay = FormatAmount(amountMinor, currency);

            string paymentMethodId = ReadString(payload, "payment_method_id");
            string intentStatus = ReadString(payload, "intent_status");
            string chargeId = ReadString(payload, "charge_id");
            string challengeType = ReadString(payload, "challenge_type");
            bool manualActionRequired = ReadBool(payload, "manual_action_required")
                || string.Equals(rawState, "challenge_required", StringComparison.OrdinalIgnoreCase);

            string code = ReadString(payload, "code");
            string declineCode = ReadString(payload, "decline_code");
            string errorCode = ReadString(payload, "error_code");
            string errorStage = ReadString(payload, "error_stage");
            string message = ReadString(payload, "message");
            if (message.Length == 0) message = ReadString(payload, "error");
            string failureDetail = ReadString(payload, "failure_detail");

            string resolvedState;
            if (quoteOnly)
            {
                resolvedState = ok ? "quoted" : "failed";
            }
            else if (rawState.Length > 0)
            {
                resolvedState = rawState;
            }
            else if (string.Equals(status, "read_only", StringComparison.OrdinalIgnoreCase))
            {
                resolvedState = "read_only";
            }
            else
            {
                resolvedState = ok ? "paid" : "failed";
            }

            return new ProtocolCardPaymentResult(
                Ok: ok,
                QuoteOnly: quoteOnly,
                State: resolvedState,
                Mode: mode,
                SessionId: sessionId,
                CheckoutUrl: checkoutUrl,
                AmountMinor: amountMinor,
                Currency: currency,
                AmountDisplay: amountDisplay,
                PaymentMethodId: paymentMethodId,
                IntentStatus: intentStatus,
                ChargeId: chargeId,
                ChallengeType: challengeType,
                ManualActionRequired: manualActionRequired,
                Code: code,
                DeclineCode: declineCode,
                ErrorCode: errorCode,
                ErrorStage: errorStage,
                Message: SensitiveDataSanitizer.Redact(message),
                FailureDetail: SensitiveDataSanitizer.Redact(failureDetail),
                RawJson: rawJson);
        }

        public static string FormatAmount(long? amountMinor, string currency)
        {
            string code = (currency ?? "").Trim().ToUpperInvariant();
            if (!amountMinor.HasValue) return code;
            long value = amountMinor.Value;
            if (code.Length == 0)
                return value.ToString(CultureInfo.InvariantCulture);
            if (ZeroDecimalCurrencies.Contains(code))
                return $"{value.ToString(CultureInfo.InvariantCulture)} {code}";
            decimal major = value / 100m;
            return $"{major.ToString("0.00", CultureInfo.InvariantCulture)} {code}";
        }

        private static string ReadString(JsonElement obj, string name)
        {
            if (!obj.TryGetProperty(name, out JsonElement element)) return "";
            return element.ValueKind switch
            {
                JsonValueKind.String => (element.GetString() ?? "").Trim(),
                JsonValueKind.Number or JsonValueKind.True or JsonValueKind.False => (element.ToString() ?? "").Trim(),
                _ => "",
            };
        }

        private static bool ReadBool(JsonElement obj, string name)
            => obj.TryGetProperty(name, out JsonElement element) && element.ValueKind == JsonValueKind.True;

        private static long? ReadNullableLong(JsonElement obj, string name)
        {
            if (!obj.TryGetProperty(name, out JsonElement element)) return null;
            if (element.ValueKind == JsonValueKind.Number && element.TryGetInt64(out long parsed))
                return parsed;
            if (element.ValueKind == JsonValueKind.String
                && long.TryParse(element.GetString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out long fromString))
                return fromString;
            return null;
        }
    }

    /// <summary>
    /// 25-region billing country catalog aligned with
    /// <c>sms_tool/payment/protocol_codec.py::BILLING_REGIONS</c>.
    /// </summary>
    public static class ProtocolCardPaymentCatalog
    {
        public static IReadOnlyList<ProtocolCardCountryOption> BillingCountries { get; } =
        [
            new("US", "US (USD)"),
            new("JP", "JP (JPY)"),
            new("GB", "GB (GBP)"),
            new("DE", "DE (EUR)"),
            new("NL", "NL (EUR)"),
            new("PL", "PL (PLN)"),
            new("CA", "CA (CAD)"),
            new("AU", "AU (AUD)"),
            new("SG", "SG (SGD)"),
            new("KR", "KR (KRW)"),
            new("PH", "PH (PHP)"),
            new("VN", "VN (VND)"),
            new("TH", "TH (THB)"),
            new("MY", "MY (MYR)"),
            new("ID", "ID (IDR)"),
            new("IN", "IN (INR)"),
            new("TR", "TR (TRY)"),
            new("BR", "BR (BRL)"),
            new("MX", "MX (MXN)"),
            new("CL", "CL (CLP)"),
            new("AR", "AR (ARS)"),
            new("EG", "EG (EGP)"),
            new("SA", "SA (SAR)"),
            new("AE", "AE (AED)"),
            new("ZA", "ZA (ZAR)"),
        ];
    }
}
