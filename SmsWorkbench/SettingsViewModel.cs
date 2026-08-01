using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using System.Collections.ObjectModel;

namespace SmsWorkbench
{
    public sealed partial class SettingsViewModel : ObservableObject
    {
        private readonly ISettingsService _settingsService;
        private readonly IFileLauncher _fileLauncher;

        [ObservableProperty] private SettingsCategoryViewModel selectedCategory;
        [ObservableProperty] private string status = "";

        public SettingsViewModel(ISettingsService settingsService, IFileLauncher fileLauncher)
        {
            _settingsService = settingsService;
            _fileLauncher = fileLauncher;
            Categories = new ObservableCollection<SettingsCategoryViewModel>(settingsService.Load());
            selectedCategory = Categories.FirstOrDefault();
        }

        public event EventHandler CloseRequested;

        public ObservableCollection<SettingsCategoryViewModel> Categories { get; }

        public bool Saved { get; private set; }

        [RelayCommand]
        private void OpenConfig() => _fileLauncher.Open(_settingsService.ConfigPath);

        [RelayCommand]
        private void Save()
        {
            SettingsSaveResult result = _settingsService.Save(Categories);
            if (!result.Ok)
            {
                Status = result.Error;
                return;
            }
            Saved = true;
            Status = "配置已保存。";
            CloseRequested?.Invoke(this, EventArgs.Empty);
        }
    }
}
