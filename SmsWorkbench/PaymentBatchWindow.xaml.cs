namespace SmsWorkbench
{
    public partial class PaymentBatchWindow : Window
    {
        public PaymentBatchWindow(PaymentBatchViewModel viewModel)
        {
            InitializeComponent();
            DataContext = viewModel;
            Closing += (_, args) =>
            {
                if (!viewModel.IsRunning) return;
                args.Cancel = true;
                if (viewModel.RunCancelCommand.CanExecute(null))
                    viewModel.RunCancelCommand.Execute(null);
            };
        }
    }
}
