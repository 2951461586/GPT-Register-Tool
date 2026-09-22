#nullable enable

namespace SmsWorkbench;

public static class RegistrationStatusPresentation
{
    public const string PartialRegistered = "partial_registered";
    public const string PartialLabel = "半注册";

    public static bool IsPartial(string? state) =>
        string.Equals(state, PartialRegistered, StringComparison.OrdinalIgnoreCase)
        || string.Equals(state, PartialLabel, StringComparison.Ordinal);

    public static string MailboxStatus(string? state, string fallback) =>
        IsPartial(state) ? PartialLabel : state == "registered" ? "已注册" : fallback;

    public static bool NeedsAttention(PoolRow row) =>
        IsPartial(row.RegistrationStatus) || IsPartial(row.Status)
        || row.Status.Contains("待") || row.Status.Contains("缺") || row.Status.Contains("失败");
}
