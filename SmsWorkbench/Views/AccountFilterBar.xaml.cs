// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Account counters plus the search / scope / page-size / paging bar.
    ///
    /// No code-behind state on purpose: the control inherits MainWindow's
    /// DataContext, so every binding keeps its <c>Pool.*</c> path. The clear (×)
    /// button reads <see cref="PoolViewModel.HasSearchText"/> instead of being
    /// toggled by name from the window partials.
    /// </summary>
    public partial class AccountFilterBar : UserControl
    {
        public AccountFilterBar()
        {
            InitializeComponent();
        }
    }
}
