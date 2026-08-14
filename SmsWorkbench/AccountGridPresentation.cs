namespace SmsWorkbench
{
    public sealed class StatusSeverityConverter : IValueConverter
    {
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
        {
            string status = (value as string ?? "").Trim();
            if (status.Length == 0) return "neutral";

            if (PromotionStatusPresentation.IsTrialEligible(status)
                || status.Contains('✅') || status.Contains("完成") || status.Contains("已注册")
                || status.Contains("已获取") || status.Contains("已导入") || status.Contains("K12已进入")
                || status.Contains("PM已创建") || status.Contains("已设置"))
                return "success";

            if (status.Contains("失败") || status.Contains("失效") || status.Contains("掉号")
                || status.Contains("异常") || status.Contains("无RT") || status.Contains("缺失")
                || status.Contains("未获取") || status.Contains("K12未切换") || status.Contains("K12已退出"))
                return "danger";

            if (status.Contains('待') || status.Contains('缺') || status.Contains("OTP")
                || status.Contains("K12已申请") || status.Contains("旧token"))
                return "warn";

            if (status.Contains("已保存") || status.Contains("待刷新") || status.Contains("未知"))
                return "info";

            return "neutral";
        }

        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
        {
            throw new NotSupportedException();
        }
    }

    public static class PromotionStatusPresentation
    {
        public static bool IsTrialEligible(string status)
        {
            string value = (status ?? "").Trim();
            if (value.Length == 0) return false;
            return value.Contains("可试用", StringComparison.OrdinalIgnoreCase)
                && value.Contains("plus", StringComparison.OrdinalIgnoreCase);
        }

        public static int SortRank(string status)
        {
            if (IsTrialEligible(status)) return 0;
            return string.IsNullOrWhiteSpace(status) ? 2 : 1;
        }
    }

    public static class AccountGridOrdering
    {
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
                return new AccountSortValue(PromotionStatusPresentation.SortRank(promotion), promotion);
            }

            PropertyDescriptor property = TypeDescriptor.GetProperties(typeof(PoolRow))[member];
            object value = property?.GetValue(row);
            return new AccountSortValue(value == null ? 1 : 0, Convert.ToString(value, CultureInfo.CurrentCulture) ?? "");
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
