namespace SmsWorkbench
{
    public interface IProtocolPaymentDialogService
    {
        void ShowDialog(Window owner, ProtocolPaymentAccount account);
    }

    public sealed class ProtocolPaymentDialogService : IProtocolPaymentDialogService
    {
        private readonly IProtocolPaymentService _service;
        private readonly IFileLauncher _fileLauncher;

        public ProtocolPaymentDialogService(IProtocolPaymentService service, IFileLauncher fileLauncher)
        {
            _service = service;
            _fileLauncher = fileLauncher;
        }

        public void ShowDialog(Window owner, ProtocolPaymentAccount account)
        {
            var viewModel = new ProtocolPaymentViewModel(_service, _fileLauncher, account);
            new ProtocolPaymentWindow(viewModel) { Owner = owner }.ShowDialog();
        }
    }
}
