using SmsWorkbench.Localization;

namespace SmsWorkbench.Dialogs
{
    public partial class ScanOptionsDialog : Window
    {
        public ScanOptions? SelectedOptions { get; private set; }

        public ScanOptionsDialog(Window owner, int accountCount)
        {
            InitializeComponent();
            WindowThemeService.Attach(this);
            Owner = owner;
            TitleTextBlock.Text = Loc.F(
                "测活 {0} 个账号。HTTP 200 表示 AT 有效，HTTP 401 表示 AT 已失效；可勾选 401 自动重登。",
                Math.Max(1, accountCount));
            WorkerBox.Text = Math.Min(8, Math.Max(1, accountCount)).ToString();
        }

        private void Ok_Click(object sender, RoutedEventArgs e)
        {
            SelectedOptions = new ScanOptions
            {
                Workers = RegisterViewModel.ParsePositiveInt(WorkerBox.Text, 1, 8, 4),
                AutoRelogin = AutoReloginBox.IsChecked == true,
                LightweightInspect = LightweightInspectBox.IsChecked == true,
                InspectRefreshPromotion = RefreshPromotionBox.IsChecked == true,
            };
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
