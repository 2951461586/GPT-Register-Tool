using SmsWorkbench.Localization;

namespace SmsWorkbench.Dialogs
{
    public partial class SelectedRegisterOptionsDialog : Window
    {
        private readonly int selectedCount;
        private readonly int defaultWorkers;
        private readonly Action<RegisterOptions> accept;

        public SelectedRegisterOptionsDialog(
            Window owner,
            int selectedCount,
            int defaultWorkers,
            int checkedCount,
            Action<RegisterOptions> accept)
        {
            this.selectedCount = selectedCount;
            this.defaultWorkers = defaultWorkers;
            this.accept = accept;

            InitializeComponent();
            WindowThemeService.Attach(this);
            Owner = owner;

            int usable = Math.Max(1, selectedCount);
            HintTextBlock.Text = checkedCount > usable
                ? Loc.F(
                    "已选择 {0} 个可注册邮箱（勾选 {1} 个，本地排除 {2} 个已注册/半注册/记录不完整；后端还会剔除冷却/隔离/死路地址）",
                    usable,
                    checkedCount,
                    checkedCount - usable)
                : Loc.F("已选择 {0} 个邮箱", usable);
            WorkerBox.Text = Math.Max(1, defaultWorkers).ToString();
        }

        private void Ok_Click(object sender, RoutedEventArgs e)
        {
            var selected = new RegisterOptions
            {
                Source = "pool",
                Count = Math.Max(1, selectedCount),
                Workers = RegisterViewModel.ParsePositiveInt(WorkerBox.Text, 1, 20, defaultWorkers),
                Disable2fa = No2faBox.IsChecked == true,
                CheckPromotion = PromotionBox.IsChecked == true,
            };
            accept(selected);
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
