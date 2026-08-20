namespace SmsWorkbench
{
    public partial class PaymentBatchWindow : Window
    {
        public PaymentBatchWindow(PaymentBatchViewModel viewModel)
        {
            InitializeComponent();
            DataContext = viewModel;
            viewModel.PropertyChanged += (_, args) =>
            {
                if (args.PropertyName == nameof(PaymentBatchViewModel.IsPayPalSelected))
                    UpdateAuthorizationColumnVisibility(viewModel.IsPayPalSelected);
            };
            UpdateAuthorizationColumnVisibility(viewModel.IsPayPalSelected);
            Closing += (_, args) =>
            {
                if (!viewModel.IsRunning) return;
                args.Cancel = true;
                if (viewModel.RunCancelCommand.CanExecute(null))
                    viewModel.RunCancelCommand.Execute(null);
            };
        }

        private void UpdateAuthorizationColumnVisibility(bool visible)
        {
            AuthorizationQueueColumn.Visibility = visible ? Visibility.Visible : Visibility.Collapsed;
        }
    }
}
