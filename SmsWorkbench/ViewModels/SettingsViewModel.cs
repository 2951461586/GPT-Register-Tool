// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using System.Collections.ObjectModel;

using SmsWorkbench.Localization;

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
            // Load() has just refreshed the lane snapshot, so read it afterwards.
            ProxyLaneNotice = BuildProxyLaneNotice(settingsService.AuthoritativeProxyLanes);
            selectedCategory = Categories.FirstOrDefault();
            WatchProviderSelector();
        }

        /// <summary>
        /// Warning shown above the dialog's action row when <c>proxy.lanes</c>
        /// owns the effective route. Empty when every lane resolves through the
        /// legacy keys this page edits.
        /// </summary>
        public string ProxyLaneNotice { get; }

        /// <summary>
        /// Builds the notice from the lanes declared under <c>proxy.lanes</c>.
        ///
        /// <para>
        /// The backend treats a non-empty <c>proxy.lanes.&lt;lane&gt;</c> as
        /// authoritative, so editing <c>proxy.registration</c> / <c>mailbox_proxy</c>
        /// here changes nothing for that lane. Saying so is the difference between
        /// "the box is blank" and "this page is not the owner". Kept as a pure
        /// function so the text is testable without constructing a window.
        /// </para>
        /// </summary>
        internal static string BuildProxyLaneNotice(IReadOnlyList<string> lanes)
        {
            if (lanes.Count == 0) return "";
            string joined = string.Join(" / ", lanes);
            if (lanes.Contains("protocol_registration"))
            {
                // The registration boxes are wired now: Load shows the lane's
                // endpoints and Save writes them back to the lane (dual-writing the
                // legacy keys). Only the lanes without a field here still shadow
                // the page, and that has to stay visible.
                IReadOnlyList<string> others = lanes.Where(lane => lane != "protocol_registration").ToArray();
                return others.Count == 0
                    ? Loc.F(
                        "注册代理已接入 proxy.lanes（{0}）：填写后保存会直接写入该 lane（留空保存不改动 lanes），并同步旧键 proxy.registration / proxy.pool。",
                        joined)
                    : Loc.F(
                        "注册代理已接入 proxy.lanes（{0}）：填写后保存会直接写入该 lane（留空保存不改动 lanes）并同步旧键；{1} 在本页没有字段，仍以 proxy.lanes 为准。",
                        joined,
                        string.Join(" / ", others));
            }
            return Loc.F(
                "代理路线以 proxy.lanes 中的 {0} 为准；本页的邮箱代理只写旧键，不会覆盖 lanes。",
                joined);
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
