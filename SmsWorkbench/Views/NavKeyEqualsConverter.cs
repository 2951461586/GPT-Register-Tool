// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// True when a sidebar row's <c>CommandParameter</c> equals the sidebar's
    /// <see cref="Sidebar.ActiveKey"/>.
    ///
    /// Used as a MultiBinding inside <c>IconNavButtonStyle</c> rather than a
    /// code-behind walk over the visual tree: the comparison then lives with the
    /// style that consumes it, and the nineteen rows need no per-row binding.
    /// An empty key (idle) never matches, so no row is ever highlighted by accident.
    /// </summary>
    public sealed class NavKeyEqualsConverter : IMultiValueConverter
    {
        public object Convert(object[] values, Type targetType, object parameter, CultureInfo culture)
            => values.Length == 2
                && values[0] is string rowKey
                && values[1] is string activeKey
                && rowKey.Length > 0
                && string.Equals(rowKey, activeKey, StringComparison.Ordinal);

        public object[] ConvertBack(object value, Type[] targetTypes, object parameter, CultureInfo culture)
            => throw new NotSupportedException("NavKeyEqualsConverter is one-way: the active row is derived from state.");
    }
}
