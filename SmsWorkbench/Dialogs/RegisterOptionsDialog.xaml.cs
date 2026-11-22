using SmsWorkbench.Localization;

namespace SmsWorkbench.Dialogs
{
    public partial class RegisterOptionsDialog : Window
    {
        private readonly int defaultWorkers;
        private readonly Action<RegisterOptions> accept;
        private readonly Action<int>? onCountChosen;

        public RegisterOptionsDialog(
            Window owner,
            int initialCount,
            int defaultWorkers,
            Action<RegisterOptions> accept,
            Action<int>? onCountChosen)
        {
            this.defaultWorkers = defaultWorkers;
            this.accept = accept;
            this.onCountChosen = onCountChosen;

            InitializeComponent();
            WindowThemeService.Attach(this);
            Owner = owner;
            CountBox.Text = Math.Max(1, initialCount).ToString();
            WorkerBox.Text = Math.Max(1, defaultWorkers).ToString();
            UpdateTargetControls();
        }

        private void SourceBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
        {
            if (CountLabel != null)
            {
                UpdateTargetControls();
            }
        }

        private void UpdateTargetControls()
        {
            bool targetMode = string.Equals(
                (SourceBox.SelectedItem as ComboBoxItem)?.Tag as string,
                "remail_target",
                StringComparison.OrdinalIgnoreCase);
            CountLabel.Text = targetMode ? Loc.T("注册数量") : Loc.T("数量");
        }

        private void Ok_Click(object sender, RoutedEventArgs e)
        {
            int count = RegisterViewModel.ParsePositiveInt(CountBox.Text, 1, 200, 1);
            int workers = RegisterViewModel.ParsePositiveInt(WorkerBox.Text, 1, 20, defaultWorkers);
            string selectedSource = ((SourceBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "pool";
            var selected = new RegisterOptions
            {
                Source = selectedSource,
                Count = count,
                Workers = workers,
                Disable2fa = No2faBox.IsChecked == true,
                CheckPromotion = PromotionBox.IsChecked == true,
            };
            accept(selected);
            onCountChosen?.Invoke(count);
            DialogResult = true;
            Close();
        }

        private void Cancel_Click(object sender, RoutedEventArgs e)
        {
            DialogResult = false;
            Close();
        }
    }
}
