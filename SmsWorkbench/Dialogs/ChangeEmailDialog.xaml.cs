using SmsWorkbench.Localization;

namespace SmsWorkbench.Dialogs
{
    public partial class ChangeEmailDialog : Window
    {
        private readonly int initialWorkers;
        private readonly string smailrDomain;
        private readonly string cfworkerDomain;

        internal ChangeEmailDialogOptions? SelectedOptions { get; private set; }

        public ChangeEmailDialog(
            Window owner,
            int count,
            int defaultWorkers,
            string smailrDomain,
            string cfworkerDomain)
        {
            initialWorkers = Math.Min(defaultWorkers, Math.Max(1, count));
            this.smailrDomain = smailrDomain ?? string.Empty;
            this.cfworkerDomain = cfworkerDomain ?? string.Empty;

            InitializeComponent();
            WindowThemeService.Attach(this);
            Owner = owner;
            ProviderLabelTextBlock.Text = Loc.F("目标邮箱 provider（{0} 个账号）", count);
            WorkerTextBox.Text = initialWorkers.ToString(CultureInfo.InvariantCulture);
        }

        private void BrowseButton_Click(object sender, RoutedEventArgs e)
        {
            var dialog = new Microsoft.Win32.OpenFileDialog
            {
                Filter = Loc.T("文本文件 (*.txt)|*.txt|所有文件 (*.*)|*.*"),
            };
            if (dialog.ShowDialog() == true)
            {
                FileTextBox.Text = dialog.FileName;
            }
        }

        private void OkButton_Click(object sender, RoutedEventArgs e)
        {
            string provider = (ProviderComboBox.SelectedItem as ComboBoxItem)?.Tag as string ?? string.Empty;
            if (string.IsNullOrWhiteSpace(provider))
            {
                return;
            }

            int workers = int.TryParse(WorkerTextBox.Text, out int parsed)
                ? Math.Clamp(parsed, 1, 16)
                : initialWorkers;
            SelectedOptions = new ChangeEmailDialogOptions(
                provider,
                workers,
                FileTextBox.Text.Trim(),
                smailrDomain,
                cfworkerDomain);
            DialogResult = true;
            Close();
        }

        private void CancelButton_Click(object sender, RoutedEventArgs e)
        {
            DialogResult = false;
            Close();
        }
    }
}
