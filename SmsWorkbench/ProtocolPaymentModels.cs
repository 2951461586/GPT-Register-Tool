using System.Text.Json;

namespace SmsWorkbench
{
    public sealed record ProtocolPaymentAccount(string Email, string SessionFile);

    public sealed class ProtocolPaymentPreferences
    {
        public string Method { get; set; } = "paypal";
        public string TargetCountry { get; set; } = "US";
        public string CheckoutCountry { get; set; } = "US";
        public string ApproveCountry { get; set; } = "TR";
        public string UpdateCountry { get; set; } = "TR";

        public string Signature() => string.Join("|", Method, TargetCountry, CheckoutCountry, ApproveCountry, UpdateCountry);
    }

    internal sealed class ProtocolPaymentHistoryEntry
    {
        public string SavedAt { get; set; } = "";
        public string Signature { get; set; } = "";
        public ProtocolPaymentPreferences Selection { get; set; } = new();
    }

    internal sealed class ProtocolPaymentHistoryFile
    {
        public ProtocolPaymentPreferences Last { get; set; } = new();
        public List<ProtocolPaymentHistoryEntry> History { get; set; } = new();
    }

    public sealed record ProtocolPaymentRequest(
        string PaymentMethod,
        string AccessToken,
        string TargetCountry,
        string CheckoutProxyPool,
        string ApproveProxyPool,
        bool JitRefresh,
        bool ProbeOnly,
        bool RequireZero,
        bool RequireBaToken,
        string BlikCode,
        string CheckoutCountry,
        string ApproveCountry,
        string UpdateCountry,
        ProtocolPaymentAccount Account);

    public sealed record ProtocolPaymentRunResult(ProtocolPaymentResultPresentation Presentation, string Error = "");
}
