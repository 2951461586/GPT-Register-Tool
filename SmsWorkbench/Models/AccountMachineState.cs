// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// Row decisions (badge colour, registered / needs-registration / attention /
    /// exportable) keyed off the backend's **machine state**, with the display
    /// label used only for rows written before that field existed.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Why this exists: five separate places decided something about an account row
    /// by substring-matching the label the grid renders
    /// (<c>AccountGridPresentation.StatusSeverityConverter</c>,
    /// <c>RegisterViewModel.HasRegisteredAccountState</c>,
    /// <c>ExportViewModel.IsImportable</c>,
    /// <c>PoolViewModel.IsRegistered</c>,
    /// <c>RegistrationStatusPresentation.NeedsAttention</c>). The label is display
    /// copy: rewording it, or switching the UI language, silently changed which
    /// rows counted as registered, which were offered for registration again and
    /// which badges were red. The 2026-10-26 fix taught each predicate every
    /// shipped language's wording, which removed the *language* variable but left
    /// the root cause: the decision still parsed prose.
    /// </para>
    /// <para>
    /// Every backend row already carries the facts:
    /// <c>registration_status</c> (<see cref="PoolRow.RegistrationStatus"/>),
    /// <c>refresh_token_status</c>, <c>at_probe_status_code</c>,
    /// <c>promotion_state</c>, <c>payment_qualification_state</c> and the TOTP
    /// booleans. Those are the inputs here. A row that carries a machine value
    /// **ignores its label entirely** — that is the property the tests pin, and it
    /// is the one a reword breaks today.
    /// </para>
    /// <para>
    /// The label fallback is deliberately retained for legacy rows: session JSON
    /// written before <c>registration_status</c> existed has no machine value, and
    /// dropping the fallback would render those rows neutral and countable-as-
    /// registered-again. Same shape as
    /// <c>PaymentQualificationSeverityConverter.SeverityFor(state, label)</c>, which
    /// established this pattern on 2026-10-28.
    /// </para>
    /// </remarks>
    public static class AccountMachineState
    {
        // ── severity vocabulary (maps to BadgeSuccess/Warn/Danger/Info/Neutral) ──
        public const string Success = "success";
        public const string Warn = "warn";
        public const string Danger = "danger";
        public const string Info = "info";
        public const string Neutral = "neutral";

        // ── registration_status vocabulary (sms_tool registration_state) ─────────
        public const string Registered = "registered";
        public const string PartialRegistered = "partial_registered";
        public const string AtProbePending = "at_probe_pending";
        public const string Pending = "pending";
        public const string RetryPending = "retry_pending";
        public const string Failed = "failed";
        public const string DeadEnd = "dead_end";
        public const string Quarantined = "quarantined";
        public const string Cancelled = "cancelled";
        public const string BatchPaused = "batch_paused";

        // ── refresh_token_status vocabulary ──────────────────────────────────────
        public const string OAuthPresent = "oauth_present";
        public const string LegacyPresent = "legacy_present";
        public const string NoRefreshToken = "no_rt";
        public const string MissingRefreshToken = "missing";

        // ── promotion_state vocabulary (sms_tool/promotion_states.py) ────────────
        public const string PromotionTrialEligible = "trial_eligible";
        public const string PromotionSubscribed = "subscribed";
        public const string PromotionFree = "free";
        public const string PromotionAuthInvalid = "auth_invalid";
        public const string PromotionProbeFailed = "probe_failed";
        public const string PromotionUnknown = "unknown";

        /// <summary>The row's <c>registration_status</c>, normalised; empty when absent.</summary>
        public static string RegistrationState(PoolRow row)
            => (row.RegistrationStatus ?? "").Trim().ToLowerInvariant();

        // ── badge severity ───────────────────────────────────────────────────────

        /// <summary>Severity for the account <c>Status</c> badge.</summary>
        public static string StatusSeverity(PoolRow row)
        {
            switch (RegistrationState(row))
            {
                case Registered:
                    return Success;
                case PartialRegistered:
                    return Warn;
                case AtProbePending:
                case Pending:
                case RetryPending:
                    return Warn;
                case Failed:
                case DeadEnd:
                case Quarantined:
                case Cancelled:
                case BatchPaused:
                    return Danger;
                case "":
                    // Legacy row: fall back to the composed label.
                    return StatusSeverityConverter.SeverityFor(row.Status);
                default:
                    return StatusSeverityConverter.SeverityFor(row.Status);
            }
        }

        /// <summary>Severity for the access-token badge (machine: presence + probe code).</summary>
        public static string AccessTokenSeverity(PoolRow row)
        {
            if (!row.HasAccessToken) return Danger;
            string code = (row.AccessTokenProbeStatusCode ?? "").Trim();
            if (code.Length == 0) return Success;
            return code == "200" ? Success : Danger;
        }

        /// <summary>Severity for the refresh-token badge (machine: the raw code).</summary>
        public static string RefreshTokenSeverity(PoolRow row)
        {
            switch ((row.RefreshTokenStatus ?? "").Trim().ToLowerInvariant())
            {
                case OAuthPresent:
                    return Success;
                case LegacyPresent:
                    return Warn;
                case NoRefreshToken:
                case MissingRefreshToken:
                    return Danger;
                case "":
                    return Neutral;
                default:
                    return StatusSeverityConverter.SeverityFor(row.RefreshTokenStatus);
            }
        }

        /// <summary>Severity for the 2FA badge (machine: the TOTP booleans).</summary>
        public static string TwoFactorSeverity(PoolRow row)
            => row.TwoFactorEnabled ? Success : Warn;

        /// <summary>Severity for the 优惠状态 badge (machine: <c>promotion_state</c>).</summary>
        public static string PromotionSeverity(PoolRow row)
        {
            switch ((row.PromotionState ?? "").Trim().ToLowerInvariant())
            {
                case PromotionTrialEligible:
                    return Success;
                case PromotionSubscribed:
                    return Info;
                case PromotionFree:
                    return Neutral;
                case PromotionAuthInvalid:
                case PromotionProbeFailed:
                    return Danger;
                case PromotionUnknown:
                    return Info;
                case "":
                    return StatusSeverityConverter.SeverityFor(row.PromotionStatus);
                default:
                    return StatusSeverityConverter.SeverityFor(row.PromotionStatus);
            }
        }

        // ── row predicates ───────────────────────────────────────────────────────

        /// <summary>
        /// The account exists: a session row, an indexed account, a machine
        /// <c>registered</c> state, or (legacy) a label that says so.
        /// </summary>
        public static bool IsRegistered(PoolRow row)
        {
            if (IsPartial(row)) return false;
            if ((row.AccountType ?? "").Contains("Session")) return true;
            if ((row.SourcePath ?? "").EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase)) return true;
            return StatusSaysRegistered(row);
        }

        /// <summary>
        /// True when the row's own facts say it is already registered.
        /// A machine value wins over the label in both directions: a row marked
        /// <c>pending</c> stays unregistered even if its label still reads
        /// "已注册" (a stale row), and a row marked <c>registered</c> counts even
        /// when the label was reworded.
        /// </summary>
        public static bool StatusSaysRegistered(PoolRow row)
        {
            switch (RegistrationState(row))
            {
                case Registered:
                    return true;
                case PartialRegistered:
                case AtProbePending:
                case Pending:
                case RetryPending:
                case Failed:
                case DeadEnd:
                case Quarantined:
                case Cancelled:
                case BatchPaused:
                    return false;
            }
            return LegacyLabelSaysRegistered(row);
        }

        /// <summary>True when the row still needs a registration run.</summary>
        public static bool NeedsRegistration(PoolRow row)
            => row != null && !IsPartial(row) && !StatusSaysRegistered(row);

        /// <summary>True when the row belongs in the "needs attention" filter.</summary>
        public static bool NeedsAttention(PoolRow row)
        {
            if (IsPartial(row)) return true;
            switch (RegistrationState(row))
            {
                case PartialRegistered:
                case AtProbePending:
                case Pending:
                case RetryPending:
                case Failed:
                case DeadEnd:
                case Quarantined:
                case Cancelled:
                case BatchPaused:
                    return true;
                case Registered:
                    // A machine "registered" row can still need attention when its
                    // token is gone; the badge already shows danger for that.
                    return RefreshTokenSeverity(row) == Danger;
                case "":
                    break;
                default:
                    return true;
            }
            // Legacy rows: the former display-text rule, unchanged.
            return Loc.DisplayContains(row.Status, "待")
                || Loc.DisplayContains(row.Status, "缺")
                || Loc.DisplayContains(row.Status, "失败")
                || RefreshTokenSeverity(row) == Danger;
        }

        /// <summary>True when the row carries an account an importer can consume.</summary>
        public static bool CanExport(PoolRow row)
        {
            if (row.HasAccessToken) return true;
            if (StatusSaysRegistered(row)) return true;
            // A registered-but-unprobed row (payment link present) is still
            // exportable; the state bit for that is the machine payment field.
            return (row.RegistrationStatus ?? "").Trim().Length > 0
                && (row.PaymentQualificationState ?? "").Trim().Length > 0;
        }

        /// <summary>Half-registered rows, machine first then the legacy label.</summary>
        public static bool IsPartial(PoolRow row)
            => RegistrationStatusPresentation.IsPartial(row.RegistrationStatus)
                || RegistrationStatusPresentation.IsPartial(row.Status);

        private static bool LegacyLabelSaysRegistered(PoolRow row)
        {
            string status = row.Status ?? "";
            return Loc.DisplayContains(status, "已注册")
                || Loc.DisplayContains(status, "支付完成")
                || Loc.DisplayContains(status, "已导入")
                || status.Contains("PayPal", StringComparison.OrdinalIgnoreCase);
        }
    }
}
