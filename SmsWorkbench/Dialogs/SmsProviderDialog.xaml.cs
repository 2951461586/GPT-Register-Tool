// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// One-click SMS dialog. XAML since 2026-11-20; the country → tier cascade and
    /// the inventory wording live in <see cref="SmsProviderDialogViewModel"/>.
    /// </summary>
    /// <remarks>
    /// The window is clamped to the work area here rather than in XAML: a 620px
    /// dialog on a 600px-wide screen must still open, and `SystemParameters` is a
    /// screen fact, not a layout one.
    /// </remarks>
    internal partial class SmsProviderDialog : Window
    {
        public SmsProviderDialog(Window owner, SmsProviderDialogViewModel viewModel)
        {
            InitializeComponent();
            Owner = owner;
            DataContext = viewModel;
            Width = Math.Min(620, SystemParameters.WorkArea.Width - 60);
        }

        private void Start_Click(object sender, RoutedEventArgs e)
        {
            if (DataContext is SmsProviderDialogViewModel { CanStart: true })
            {
                DialogResult = true;
                return;
            }
            // Enter on a dialog whose catalog could not produce a pair must not
            // close it as "confirmed": the button is disabled, and this guard keeps
            // the keyboard path honest.
            DialogResult = false;
        }
    }
}
