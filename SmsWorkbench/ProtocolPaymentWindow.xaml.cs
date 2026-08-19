namespace SmsWorkbench
{
    public partial class ProtocolPaymentWindow : Window
    {
        private readonly ProtocolPaymentViewModel _viewModel;

        public ProtocolPaymentWindow(ProtocolPaymentViewModel viewModel)
        {
            InitializeComponent();
            _viewModel = viewModel ?? throw new ArgumentNullException(nameof(viewModel));
            DataContext = viewModel;
            Closing += OnClosing;
            Closed += (_, __) => _viewModel.Dispose();
        }

        private void Close_Click(object sender, RoutedEventArgs e) => Close();

        private void OnClosing(object sender, CancelEventArgs e)
        {
            if (!_viewModel.IsRunning) return;
            e.Cancel = true;
            _viewModel.CancelCommand.Execute(null);
        }
    }
}
