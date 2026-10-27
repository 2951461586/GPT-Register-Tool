// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// True when a control is narrower than <see cref="Threshold"/>, i.e. the account
    /// table has to become a card list.
    ///
    /// Bound to the grid's own <c>ActualWidth</c>, not the window's: the sidebar takes
    /// 248 px (or 56 collapsed), so a 1220 px window leaves roughly 940 px for the
    /// table, and the table needs about that much to show its A-tier columns without
    /// horizontal scrolling. Threshold is a property on the converter, so a future
    /// re-tune is one number and both views already read it.
    /// </summary>
    public sealed class NarrowWidthConverter : IValueConverter
    {
        /// <summary>Content width (px) below which the card list replaces the table.</summary>
        public const double Threshold = 960;

        public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
            => value is double width && width > 0 && width < Threshold;

        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
            => throw new NotSupportedException("NarrowWidthConverter is one-way: the layout is derived from width.");
    }
}
