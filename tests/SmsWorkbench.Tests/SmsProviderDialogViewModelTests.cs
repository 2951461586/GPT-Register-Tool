namespace SmsWorkbench.Tests;

/// <summary>
/// The country → tier cascade and the inventory line were lambdas inside
/// <c>SmsProviderDialogService.ShowOneClickDialogAsync</c> until 2026-11-20, so
/// the only way to observe them was to open the dialog. They now live in
/// <see cref="SmsProviderDialogViewModel"/> and are driven directly here.
/// </summary>
public sealed class SmsProviderDialogViewModelTests
{
    private static SmsProviderCountryChoice Country(string id, params (string Price, int Count)[] tiers) =>
        new(id, id.ToUpperInvariant(), id, tiers
            .Select(tier => new SmsProviderPriceTier(tier.Price, tier.Count))
            .ToArray());

    private static SmsProviderDialogViewModel ViewModel() => new(
        "SMSBower",
        "12.34",
        new[]
        {
            Country("38", ("0.054", 120), ("0.060", 3)),
            Country("6", ("0.090", SmsProviderPriceTier.UnknownCount)),
        },
        Country("38", ("0.054", 120), ("0.060", 3)),
        null);

    [Fact]
    public void HeadingAndBalanceNameTheProviderAndTheLiveBalance()
    {
        var viewModel = ViewModel();

        Assert.Equal(Localization.Loc.F("选择 {0} 号码", "SMSBower"), viewModel.Heading);
        Assert.Equal(Localization.Loc.F("当前平台余额：${0}", "12.34"), viewModel.BalanceText);
    }

    [Fact]
    public void TheFirstTierIsPreselectedForTheFirstCountry()
    {
        var viewModel = ViewModel();

        Assert.Equal(new[] { "0.054", "0.060" }, viewModel.Tiers.Select(tier => tier.Price));
        Assert.Equal("0.054", viewModel.SelectedTier?.Price);
        Assert.True(viewModel.CanStart);
    }

    [Fact]
    public void ChangingTheCountryResetsTheTierToThatCountryFirstEntry()
    {
        var viewModel = ViewModel();

        viewModel.SelectedCountry = viewModel.Countries[1];

        Assert.Equal(new[] { "0.090" }, viewModel.Tiers.Select(tier => tier.Price));
        Assert.True(
            viewModel.SelectedTier?.Price == "0.090",
            "a stale tier from the old country must not survive");
    }

    [Fact]
    public void AQueriedTierShowsStockAndPrice()
    {
        var viewModel = ViewModel();

        Assert.Equal(
            Localization.Loc.F("当前库存 {0} 个，价格 ${1} / 个", 120, "0.054"),
            viewModel.InventoryText);
    }

    [Fact]
    public void AConfigDerivedTierShowsThePriceOnly()
    {
        // `UnknownCount` is -1: the tier came from config, so claiming a stock
        // figure would be a lie, and "(unqueried)" next to the price read as a
        // caveat on the price itself.
        var viewModel = ViewModel();

        viewModel.SelectedCountry = viewModel.Countries[1];

        Assert.Equal(Localization.Loc.F("价格 ${0} / 个", "0.090"), viewModel.InventoryText);
    }

    [Fact]
    public void TheInventoryLineFollowsTheTierSelection()
    {
        var viewModel = ViewModel();
        viewModel.SelectedTier = viewModel.Tiers[1];

        Assert.Equal(
            Localization.Loc.F("当前库存 {0} 个，价格 ${1} / 个", 3, "0.060"),
            viewModel.InventoryText);
    }

    [Fact]
    public void AnEmptySelectionCannotBeConfirmed()
    {
        var viewModel = new SmsProviderDialogViewModel(
            "SMSBower",
            "0",
            Array.Empty<SmsProviderCountryChoice>(),
            new SmsProviderCountryChoice("38", "38", "", Array.Empty<SmsProviderPriceTier>()),
            null);

        Assert.False(viewModel.CanStart);
    }
}
