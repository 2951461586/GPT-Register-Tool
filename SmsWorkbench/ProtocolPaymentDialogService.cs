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
        private readonly IStageMatrixStore _stageMatrixStore;

        public ProtocolPaymentDialogService(IProtocolPaymentService service, IFileLauncher fileLauncher, IStageMatrixStore stageMatrixStore)
        {
            _service = service;
            _fileLauncher = fileLauncher;
            _stageMatrixStore = stageMatrixStore;
        }

        public void ShowDialog(Window owner, ProtocolPaymentAccount account)
        {
            var viewModel = new ProtocolPaymentViewModel(_service, _fileLauncher, account, _stageMatrixStore);
            new ProtocolPaymentWindow(viewModel) { Owner = owner }.ShowDialog();
        }
    }
}
