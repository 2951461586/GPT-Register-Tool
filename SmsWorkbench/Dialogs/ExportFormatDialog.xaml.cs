namespace SmsWorkbench.Dialogs
{
    /// <summary>
    /// Export-format picker. Converted from the hand-built dialog inside
    /// <see cref="ExportDialogService.ShowExportFormat"/> on 2026-11-20, so the
    /// layout is editable in XAML instead of 90 lines of `new Grid/ComboBox`.
    /// </summary>
    /// <remarks>
    /// The default is CPA (<c>SelectedIndex="2"</c>) because that is what the code
    /// dialog preselected; the first two entries stay because operators export
    /// mailbox lists and raw sessions more often than anything else.
    ///
    /// <see cref="SelectedFormat"/> is the tag of the chosen item, or an empty
    /// string when the operator cancels — the contract
    /// <see cref="IExportDialogService.ShowExportFormat"/> documents.
    /// </remarks>
    public partial class ExportFormatDialog : Window
    {
        /// <summary>Format tag from the picker; empty when cancelled.</summary>
        public string SelectedFormat { get; private set; } = "";

        public ExportFormatDialog()
        {
            InitializeComponent();
            WindowThemeService.Attach(this);
        }

        private void Export_Click(object sender, RoutedEventArgs e)
        {
            SelectedFormat = ((FormatBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "cpa";
            DialogResult = true;
        }

        private void Cancel_Click(object sender, RoutedEventArgs e)
        {
            SelectedFormat = "";
            DialogResult = false;
        }
    }
}
