namespace SmsWorkbench.Views
{
    public partial class SettingsWindow : Window
    {
        public SettingsWindow(SettingsViewModel viewModel)
        {
            InitializeComponent();
            WindowThemeService.Attach(this);
            DataContext = viewModel;
            viewModel.CloseRequested += (_, _) => Close();
        }
    }
}
