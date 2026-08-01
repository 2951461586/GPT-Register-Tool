namespace SmsWorkbench
{
    public interface IPaymentBatchDialogService
    {
        bool ShowDialog(Window owner, IEnumerable<PaymentBatchAccount> accounts);
    }

    public sealed class PaymentBatchDialogService : IPaymentBatchDialogService
    {
        private readonly IPaymentBatchService _paymentBatchService;
        private readonly IFileLauncher _fileLauncher;

        public PaymentBatchDialogService(IPaymentBatchService paymentBatchService, IFileLauncher fileLauncher)
        {
            _paymentBatchService = paymentBatchService;
            _fileLauncher = fileLauncher;
        }

        public bool ShowDialog(Window owner, IEnumerable<PaymentBatchAccount> accounts)
        {
            var viewModel = new PaymentBatchViewModel(_paymentBatchService, _fileLauncher, accounts);
            var window = new PaymentBatchWindow(viewModel) { Owner = owner };
            window.ShowDialog();
            return viewModel.HasRun;
        }
    }
}
