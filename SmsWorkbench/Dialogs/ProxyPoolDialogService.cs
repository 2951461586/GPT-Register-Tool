// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    public interface IProxyPoolDialogService
    {
        void ShowDialog(Window owner);
    }

    /// <summary>
    /// Opens the proxy-pool admin window. A dialog service (rather than the
    /// window constructing itself from the main window) keeps
    /// <see cref="MainWindow"/> out of the window/viewmodel wiring, matching
    /// <see cref="PaymentBatchDialogService"/>.
    /// </summary>
    public sealed class ProxyPoolDialogService : IProxyPoolDialogService
    {
        private readonly IProxyPoolService _proxyPoolService;

        public ProxyPoolDialogService(IProxyPoolService proxyPoolService)
        {
            _proxyPoolService = proxyPoolService;
        }

        public void ShowDialog(Window owner)
        {
            var viewModel = new ProxyPoolViewModel(_proxyPoolService);
            var window = new ProxyPoolWindow(viewModel) { Owner = owner };
            window.ShowDialog();
        }
    }
}
