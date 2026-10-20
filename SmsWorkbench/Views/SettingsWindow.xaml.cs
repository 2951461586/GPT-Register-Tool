// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    public partial class SettingsWindow : Window
    {
        public SettingsWindow(SettingsViewModel viewModel)
        {
            InitializeComponent();
            DataContext = viewModel;
            viewModel.CloseRequested += (_, _) => Close();
        }
    }
}
