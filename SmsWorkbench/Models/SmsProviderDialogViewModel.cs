using SmsWorkbench.Localization;

namespace SmsWorkbench.Models
{
    /// <summary>
    /// State behind the one-click SMS dialog: provider heading, balance, the
    /// country/tier pair and the inventory line under the tier picker.
    /// </summary>
    /// <remarks>
    /// Extracted from <c>SmsProviderDialogService.ShowOneClickDialogAsync</c> on
    /// 2026-11-20, where the same three facts lived in three `TextBlock`/`ComboBox`
    /// instances wired by lambdas. The country → tier cascade and the inventory
    /// wording (queried vs config-derived price) are rule-bearing, so they belong
    /// where a test can drive them without a window.
    /// </remarks>
    internal sealed partial class SmsProviderDialogViewModel : ObservableObject
    {
        public SmsProviderDialogViewModel(
            string providerLabel,
            string balance,
            IReadOnlyList<SmsProviderCountryChoice> countries,
            SmsProviderCountryChoice selectedCountry,
            SmsProviderPriceTier? selectedTier)
        {
            Countries = countries;
            Heading = Loc.F("选择 {0} 号码", providerLabel);
            BalanceText = Loc.F("当前平台余额：${0}", balance);
            this.selectedCountry = selectedCountry;
            this.selectedTier = selectedTier ?? selectedCountry.Tiers.FirstOrDefault();
        }

        public string Heading { get; }

        public string BalanceText { get; }

        public IReadOnlyList<SmsProviderCountryChoice> Countries { get; }

        [ObservableProperty]
        private SmsProviderCountryChoice selectedCountry;

        /// <summary>Tiers of the selected country; the picker rebinds when the country changes.</summary>
        public IReadOnlyList<SmsProviderPriceTier> Tiers => SelectedCountry?.Tiers ?? Array.Empty<SmsProviderPriceTier>();

        [ObservableProperty]
        private SmsProviderPriceTier? selectedTier;

        /// <summary>
        /// The line under the tier picker. A config-derived tier has no stock figure
        /// (<see cref="SmsProviderPriceTier.UnknownCount"/>), and an "(unqueried)"
        /// note next to the price read as a caveat on the price itself — so that
        /// case prints the price only.
        /// </summary>
        public string InventoryText => SelectedTier is null
            ? ""
            : SelectedTier.Count < 0
                ? Loc.F("价格 ${0} / 个", SelectedTier.Price)
                : Loc.F("当前库存 {0} 个，价格 ${1} / 个", SelectedTier.Count, SelectedTier.Price);

        /// <summary>False when the fallback catalog could not produce a full pair.</summary>
        public bool CanStart => SelectedCountry is not null && SelectedTier is not null;

        /// <summary>What the caller persists when the operator confirms.</summary>
        public SmsProviderCountryChoice ChosenCountry => SelectedCountry;

        public SmsProviderPriceTier? ChosenTier => SelectedTier;

        partial void OnSelectedCountryChanged(SmsProviderCountryChoice value)
        {
            OnPropertyChanged(nameof(Tiers));
            // A country with no tiers cannot happen for an online catalog and is
            // rejected before the dialog opens for the config fallback; keep the
            // reset defensive so a partial catalog degrades to "nothing selected".
            SelectedTier = value?.Tiers.FirstOrDefault();
            OnPropertyChanged(nameof(CanStart));
        }

        partial void OnSelectedTierChanged(SmsProviderPriceTier? value)
        {
            OnPropertyChanged(nameof(InventoryText));
            OnPropertyChanged(nameof(CanStart));
        }
    }
}
