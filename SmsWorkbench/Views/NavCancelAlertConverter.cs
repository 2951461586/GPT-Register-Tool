// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// True for the one row that must read as dangerous while work is in flight:
    /// "取消批次", and only while some action is actually running.
    ///
    /// The row is a cancel affordance, so it earns the accent colour the doctrine
    /// otherwise reserves for state — but only in the state that matters. Tinting it
    /// permanently would spend colour on structure, and there is nothing to cancel
    /// when the sidebar is idle (<see cref="Sidebar.ActiveKey"/> empty). The row is
    /// identified by its navigation key, the same contract the rest of
    /// <c>NavActions</c> uses.
    /// </summary>
    public sealed class NavCancelAlertConverter : IMultiValueConverter
    {
        /// <summary>Navigation key of the batch-cancel row (<c>MainWindow.NavActions</c>).</summary>
        public const string CancelKey = "cancelbatch";

        public object Convert(object[] values, Type targetType, object parameter, CultureInfo culture)
            => values.Length == 2
                && values[0] is string rowKey
                && values[1] is string activeKey
                && activeKey.Length > 0
                && string.Equals(rowKey, CancelKey, StringComparison.Ordinal);

        public object[] ConvertBack(object value, Type[] targetTypes, object parameter, CultureInfo culture)
            => throw new NotSupportedException("NavCancelAlertConverter is one-way: the tint is derived from state.");
    }
}
