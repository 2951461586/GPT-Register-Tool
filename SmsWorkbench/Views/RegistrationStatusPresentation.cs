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

    /// <summary>
    /// True when the row belongs in the "needs attention" filter and counter.
    /// </summary>
    /// <remarks>
    /// Delegates to <see cref="AccountMachineState.NeedsAttention"/> since
    /// 2026-11-20: the predicate now reads <c>registration_status</c> (and the
    /// refresh-token code) and only falls back to the display text for rows
    /// written before those fields existed. The language-agnostic label matcher
    /// this used to call lives on in that fallback.
    /// </remarks>
    public static bool NeedsAttention(PoolRow row) => AccountMachineState.NeedsAttention(row);
}
