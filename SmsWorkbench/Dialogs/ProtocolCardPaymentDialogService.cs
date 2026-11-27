namespace SmsWorkbench.Dialogs
{
    public interface IProtocolCardPaymentDialogService
    {
        void ShowDialog(
            Window owner,
            string initialCheckoutUrl = "",
            string initialAccountEmail = "",
            string initialSessionFile = "");
    }

    public sealed class ProtocolCardPaymentDialogService : IProtocolCardPaymentDialogService
    {
        private readonly IProtocolCardPaymentService _service;

        public ProtocolCardPaymentDialogService(IProtocolCardPaymentService service)
        {
            _service = service;
        }

        public void ShowDialog(
            Window owner,
            string initialCheckoutUrl = "",
            string initialAccountEmail = "",
            string initialSessionFile = "")
        {
            var viewModel = new ProtocolCardPaymentViewModel(
                _service,
                initialCheckoutUrl: initialCheckoutUrl,
                initialAccountEmail: initialAccountEmail,
                initialSessionFile: initialSessionFile,
                openUrl: OpenHttpsUrl,
                copyText: Clipboard.SetText);
            var window = new ProtocolCardPaymentWindow(viewModel) { Owner = owner };
            window.ShowDialog();
        }

        private static void OpenHttpsUrl(string url)
        {
            if (!ProtocolCardPaymentViewModel.TryValidateHttpsUrl(url, out string safeUrl))
                return;
            Process.Start(new ProcessStartInfo(safeUrl) { UseShellExecute = true });
        }
    }
}
