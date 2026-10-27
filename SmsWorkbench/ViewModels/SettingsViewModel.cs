// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using System.Collections.ObjectModel;

namespace SmsWorkbench
{
    public sealed partial class SettingsViewModel : ObservableObject
    {
        private readonly ISettingsService _settingsService;
        private readonly IFileLauncher _fileLauncher;

        [ObservableProperty] private SettingsCategoryViewModel? selectedCategory;
        [ObservableProperty] private string status = "";

        public SettingsViewModel(ISettingsService settingsService, IFileLauncher fileLauncher)
        {
            _settingsService = settingsService;
            _fileLauncher = fileLauncher;
            Categories = new ObservableCollection<SettingsCategoryViewModel>(settingsService.Load());
            selectedCategory = Categories.FirstOrDefault();
            WatchProviderSelector();
        }

        /// <summary>
        /// Keep the provider-scoped boxes in step with the 供应商 dropdown.
        ///
        /// <para>
        /// 🔴 The dropdown and the API Key box are separate controls, and
        /// `SettingsService.Load` resolves provider-scoped paths only once, at
        /// open time. Without this subscription the operator picks a different
        /// provider, the box still shows the previous provider's key, and the
        /// save writes that key into the newly selected section -- see
        /// <see cref="SettingsService.ReloadProviderScopedFields"/> for the
        /// measured incident. Reloading on every selection change makes the box
        /// always show what the save would actually write.
        /// </para>
        ///
        /// <para>
        /// Deliberately a subscription on the field rather than a handler in
        /// `SettingsWindow.xaml.cs`: the fields are declared in
        /// <see cref="SettingsCatalog"/> and rendered generically, so there is
        /// no per-field control to attach to -- and this keeps the rule testable
        /// without constructing a window.
        /// </para>
        /// </summary>
        private void WatchProviderSelector()
        {
            SettingFieldViewModel? selector = AllFields().FirstOrDefault(
                field => string.Equals(field.Key, ProviderSelectorKey, StringComparison.Ordinal));
            if (selector is null) return;
            providerSelector = selector;
            selector.PropertyChanged += (_, args) =>
            {
                if (args.PropertyName == nameof(SettingFieldViewModel.Value))
                {
                    _settingsService.ReloadProviderScopedFields(AllFields(), selector.Value);
                }
            };
        }

        private IEnumerable<SettingFieldViewModel> AllFields()
            => Categories.SelectMany(category => category.Sections).SelectMany(section => section.Fields);

        /// <summary>
        /// Field key of the 供应商 dropdown. Must match the `Options(...)` key in
        /// <see cref="SettingsCatalog"/>; the provider-parity test pins that key
        /// to `phone_reuse.source`.
        /// </summary>
        internal const string ProviderSelectorKey = "phone_provider";

        // Held so the subscription is not collected; the event source is the
        // field itself, which the Categories graph keeps alive anyway.
        private SettingFieldViewModel? providerSelector;

        public event EventHandler? CloseRequested;

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
