#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench;

public static class RegistrationStatusPresentation
{
    public const string PartialRegistered = "partial_registered";
    /// <summary>Legacy value some persisted rows still carry; comparison-only.</summary>
    public const string PartialLabel = "半注册";

    /// <summary>Localized label for display; never compare against this.</summary>
    public static string PartialDisplay => Loc.T("半注册");

    public static bool IsPartial(string? state) =>
        string.Equals(state, PartialRegistered, StringComparison.OrdinalIgnoreCase)
        || string.Equals(state, PartialLabel, StringComparison.Ordinal)
        || string.Equals(state, PartialDisplay, StringComparison.Ordinal);

    public static string MailboxStatus(string? state, string fallback) =>
        IsPartial(state) ? PartialDisplay : state == "registered" ? Loc.T("已注册") : fallback;

    public static bool NeedsAttention(PoolRow row) =>
        IsPartial(row.RegistrationStatus) || IsPartial(row.Status)
        || row.Status.Contains('待') || row.Status.Contains('缺') || row.Status.Contains("失败", StringComparison.Ordinal);
}
