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
        // The attention markers are display text, so they are matched against
        // every language's form: "待/缺/失败" in the Chinese build, "Pending /
        // Missing / Failed" in the English one. Keying on the Chinese
        // characters alone made the attention counter and the "needs
        // attention" filter silently empty out after a language switch.
        || Loc.DisplayContains(row.Status, "待")
        || Loc.DisplayContains(row.Status, "缺")
        || Loc.DisplayContains(row.Status, "失败");
}
