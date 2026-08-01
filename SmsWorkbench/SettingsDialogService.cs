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
