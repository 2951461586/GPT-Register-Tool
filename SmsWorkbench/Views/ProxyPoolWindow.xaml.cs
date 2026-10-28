// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Proxy-pool admin window: one grid, three read-only backends.
    /// <para>
    /// Code-behind only since 2026-10-28: the layout, the lane-isolation banner and
    /// the result grid all live in <c>ProxyPoolWindow.xaml</c>. The previous
    /// code-built version predated <c>Themes/Controls.xaml</c>'s implicit DataGrid
    /// style and therefore re-declared the grid's chrome and wrote its own font
    /// sizes; as XAML it inherits both instead.
    /// </para>
    /// <para>
    /// Nothing here reaches into the binding surface by name: the window only sets
    /// its DataContext and calls <c>InitializeComponent</c>. Anything that needs
    /// driving belongs on <see cref="ProxyPoolViewModel"/>, which is testable
    /// without a window.
    /// </para>
    /// </summary>
    public partial class ProxyPoolWindow : Window
    {
        public ProxyPoolWindow(ProxyPoolViewModel viewModel)
        {
            DataContext = viewModel;
            InitializeComponent();
        }
    }
}
