namespace SmsWorkbench.Views
{
    public partial class ProtocolCardPaymentWindow : FluentWindow
    {
        public ProtocolCardPaymentWindow(ProtocolCardPaymentViewModel viewModel)
        {
            ArgumentNullException.ThrowIfNull(viewModel);
            DataContext = viewModel;
            InitializeComponent();
            WindowThemeService.Attach(this);
            Closing += (_, args) =>
            {
                if (!viewModel.IsRunning) return;
                args.Cancel = true;
                viewModel.CancelRun();
            };
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }
    }
}
