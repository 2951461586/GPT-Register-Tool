namespace SmsWorkbench
{
    public partial class PaymentBatchWindow : Window
    {
        public PaymentBatchWindow(PaymentBatchViewModel viewModel)
        {
            InitializeComponent();
            DataContext = viewModel;
        }
    }
}
