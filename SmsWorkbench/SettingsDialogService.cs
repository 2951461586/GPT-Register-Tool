// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    public interface ISettingsDialogService
    {
        bool ShowDialog(Window owner);
    }

    public sealed class SettingsDialogService : ISettingsDialogService
    {
        private readonly ISettingsService _settingsService;
        private readonly IFileLauncher _fileLauncher;

        public SettingsDialogService(ISettingsService settingsService, IFileLauncher fileLauncher)
        {
            _settingsService = settingsService;
            _fileLauncher = fileLauncher;
        }

        public bool ShowDialog(Window owner)
        {
            var viewModel = new SettingsViewModel(_settingsService, _fileLauncher);
            var window = new SettingsWindow(viewModel) { Owner = owner };
            window.ShowDialog();
            return viewModel.Saved;
        }
    }
}
