// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System.Linq.Expressions;
using Expr = System.Linq.Expressions.Expression;

namespace SmsWorkbench
{
    public sealed class StatusSeverityConverter : IValueConverter
    {
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
        {
            string status = (value as string ?? "").Trim();
            if (status.Length == 0) return "neutral";
            if (RegistrationStatusPresentation.IsPartial(status)) return "warn";

            if (PromotionStatusPresentation.IsTrialEligible(status)
                || status.Contains('✅') || status.Contains("完成") || status.Contains("已注册")
                || status.Contains("已获取") || status.Contains("已导入") || status.Contains("K12已进入")
                || status.Contains("PM已创建") || status.Contains("已设置")
                // English display labels (language switch). The converter must
                // key off what the grid actually shows, or every badge falls
                // back to neutral and the colour never changes.
                || status.Contains("Completed", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Registered", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Present", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Imported", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Enabled", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Success", StringComparison.OrdinalIgnoreCase)
                || status.Contains("K12 joined", StringComparison.OrdinalIgnoreCase)
                || status.Contains("AT valid", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Payment completed", StringComparison.OrdinalIgnoreCase))
                return "success";

            if (status.Contains("失败") || status.Contains("失效") || status.Contains("掉号")
                || status.Contains("异常") || status.Contains("无RT") || status.Contains("缺失")
                || status.Contains("未获取") || status.Contains("K12未切换") || status.Contains("K12已退出")
                || status.Contains("Failed", StringComparison.OrdinalIgnoreCase)
                // "Network failure" / "Mailbox failure": the probe-failure statuses
                // are translated with "failure", not "Failed", so without this the
                // English badge went neutral while the Chinese one was danger.
                || status.Contains("failure", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Invalid", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Dropped", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Missing", StringComparison.OrdinalIgnoreCase)
                || status.Contains("No RT", StringComparison.OrdinalIgnoreCase)
                || status.Contains("K12 left", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Not switched", StringComparison.OrdinalIgnoreCase))
                return "danger";

            if (status.Contains('待') || status.Contains('缺') || status.Contains("OTP")
                || status.Contains("K12已申请") || status.Contains("旧token")
                || status.Contains("Pending", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Awaiting", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Legacy", StringComparison.OrdinalIgnoreCase)
                || status.Contains("K12 requested", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Not set", StringComparison.OrdinalIgnoreCase))
                return "warn";

            if (status.Contains("已保存") || status.Contains("待刷新") || status.Contains("未知")
                || status.Contains("Saved", StringComparison.OrdinalIgnoreCase)
                || status.Contains("Unknown", StringComparison.OrdinalIgnoreCase))
                return "info";

            return "neutral";
        }

        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        {
            throw new NotSupportedException();
        }
    }

    /// <summary>
    /// Badge severity for 支付资格, keyed off the machine state.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Every other badge feeds translated display copy into
    /// <see cref="StatusSeverityConverter"/> and matches substrings of it, so each
    /// new label needs a rule per language. This column already has a stable
    /// vocabulary (<c>sms_tool/payment/qualification_states.py</c>, persisted as
    /// <c>payment_qualification_state</c>), so it keys off the state: a reworded
    /// label cannot change the colour, and the English UI cannot silently fall back
    /// to the neutral grey badge.
    /// </para>
    /// <para>
    /// Measured 2026-10-28: only "AT失效" and "检测失败" matched the old rules, so
    /// 可用 / 无可用方式 / 无0元优惠 / 币种不符 / 出口被拒 / 未确定 all rendered as
    /// <c>BadgeNeutralFg</c> (#57606A) — the "everything is grey" report.
    /// </para>
    /// <para>
    /// The label is read as the second value only for rows written before the state
    /// field existed; those still get a colour instead of grey.
    /// </para>
    /// </remarks>
    public sealed class PaymentQualificationSeverityConverter : IMultiValueConverter
    {
        public object Convert(object[] values, Type targetType, object parameter, CultureInfo culture)
            => SeverityFor(
                values.Length > 0 ? values[0] as string : null,
                values.Length > 1 ? values[1] as string : null);

        public object[] ConvertBack(object value, Type[] targetTypes, object parameter, CultureInfo culture)
            => throw new NotSupportedException();

        /// <summary>Pure mapping, so the colour rule is testable without WPF.</summary>
        public static string SeverityFor(string? state, string? label)
        {
            switch ((state ?? "").Trim().ToLowerInvariant())
            {
                case "eligible": return "success";
                // A verdict, not a failure: the method is genuinely unavailable here.
                case "ineligible": return "warn";
                case "auth_invalid": return "danger";
                // Nothing was learned about the account; the fix is another exit.
                case "egress_denied": return "warn";
                case "probe_failed": return "danger";
                case "unknown": return "info";
                default: return LegacySeverity(label);
            }
        }

        /// <summary>
        /// Pre-state rows only: the same judgement over the composed label, in both
        /// languages. Ordered so the negative verdicts beat the "可用" positive.
        /// </summary>
        private static string LegacySeverity(string? label)
        {
            string text = (label ?? "").Trim();
            if (text.Length == 0)
                return "neutral";

            if (Contains(text, "无可用方式", "No payment method")
                || Contains(text, "无0元优惠", "No zero-due offer")
                || Contains(text, "币种不符", "Currency mismatch")
                || Contains(text, "地区不符", "Region mismatch")
                || Contains(text, "出口被拒", "Egress denied"))
                return "warn";

            if (Contains(text, "AT失效", "AT invalid")
                || Contains(text, "检测失败", "Check failed")
                || Contains(text, "风控拦截", "Risk blocked"))
                return "danger";

            if (Contains(text, "未确定", "Undetermined"))
                return "info";

            if (text.StartsWith("可用", StringComparison.Ordinal)
                || text.StartsWith("Available", StringComparison.OrdinalIgnoreCase))
                return "success";

            return "neutral";
        }

        private static bool Contains(string text, string chinese, string english)
            => text.Contains(chinese, StringComparison.Ordinal)
               || text.Contains(english, StringComparison.OrdinalIgnoreCase);
    }

    public static class PromotionStatusPresentation
    {
        // Machine promotion state emitted by the Python backend
        // (sms_tool/promotion_states.py). The Chinese label is display copy:
        // filtering and sorting key off the code so a label reword cannot
        // change behaviour. Rows recorded before the field existed fall back
        // to the legacy substring rule.
        public const string TrialEligibleState = "trial_eligible";

        public static bool IsTrialEligible(string status, string? state = null)
        {
            string machine = (state ?? "").Trim();
            if (machine.Length > 0) return machine == TrialEligibleState;
            return IsTrialEligibleLabel(status);
        }

        public static bool IsTrialEligibleLabel(string status)
        {
            string value = (status ?? "").Trim();
            if (value.Length == 0) return false;
            return (value.Contains("可试用", StringComparison.OrdinalIgnoreCase)
                    || value.Contains("plus trial", StringComparison.OrdinalIgnoreCase))
                && value.Contains("plus", StringComparison.OrdinalIgnoreCase);
        }

        public static int SortRank(string status, string? state = null)
        {
            if (IsTrialEligible(status, state)) return 0;
            return string.IsNullOrWhiteSpace(status) ? 2 : 1;
        }
    }

    public static class AccountGridOrdering
    {
        // Compiled property getters: sorting several hundred rows used to do a
        // TypeDescriptor property lookup (reflection) per row per keystroke.
        private static readonly Dictionary<string, Func<PoolRow, object>> PropertyGetters = BuildGetters();

        private static Dictionary<string, Func<PoolRow, object>> BuildGetters()
        {
            var getters = new Dictionary<string, Func<PoolRow, object>>(StringComparer.Ordinal);
            ParameterExpression parameter = Expr.Parameter(typeof(PoolRow), "row");
            foreach (System.Reflection.PropertyInfo property in typeof(PoolRow).GetProperties())
            {
                if (!property.CanRead || property.GetMethod == null) continue;
                System.Linq.Expressions.Expression body = Expr.Convert(
                    Expr.Property(parameter, property), typeof(object));
                getters[property.Name] = Expr.Lambda<Func<PoolRow, object>>(body, parameter).Compile();
            }
            return getters;
        }

        public static IEnumerable<PoolRow> Apply(
            IEnumerable<PoolRow> rows,
            string sortMember,
            ListSortDirection? direction)
        {
            if (rows == null) return Enumerable.Empty<PoolRow>();
            string member = (sortMember ?? "").Trim();
            if (member.Length == 0 || direction == null) return rows;

            Func<PoolRow, AccountSortValue> selector = row => SortValue(row, member);
            return direction == ListSortDirection.Descending
                ? rows.OrderByDescending(selector)
                : rows.OrderBy(selector);
        }

        private static AccountSortValue SortValue(PoolRow row, string member)
        {
            if (member.Equals(nameof(PoolRow.PromotionStatus), StringComparison.Ordinal))
            {
                string promotion = row?.PromotionStatus ?? "";
                return new AccountSortValue(PromotionStatusPresentation.SortRank(promotion, row?.PromotionState), promotion);
            }

            if (PropertyGetters.TryGetValue(member, out Func<PoolRow, object>? getter))
            {
                object? value = row == null ? null : getter(row);
                return new AccountSortValue(value == null ? 1 : 0, Convert.ToString(value, CultureInfo.CurrentCulture) ?? "");
            }
            return new AccountSortValue(1, "");
        }

        private readonly record struct AccountSortValue(int Rank, string Text) : IComparable<AccountSortValue>
        {
            public int CompareTo(AccountSortValue other)
            {
                int rank = Rank.CompareTo(other.Rank);
                return rank != 0
                    ? rank
                    : StringComparer.CurrentCultureIgnoreCase.Compare(Text, other.Text);
            }
        }
    }
}
