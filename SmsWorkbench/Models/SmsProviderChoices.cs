// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench
{
    // Moved out of `SmsProviderCatalogClient.cs` (P7-B) so the wire client can be
    // deleted once the dialog reads the catalog through the Python channel: the
    // dialog's view types must not live in the file that is going away.
    internal sealed class SmsProviderCountryChoice
    {
        internal SmsProviderCountryChoice(
            string id,
            string englishName,
            string chineseName,
            IReadOnlyList<SmsProviderPriceTier> tiers)
        {
            Id = id;
            EnglishName = string.IsNullOrWhiteSpace(englishName) ? id : englishName;
            ChineseName = chineseName ?? "";
            Tiers = tiers;
        }

        public string Id { get; }
        public string EnglishName { get; }
        public string ChineseName { get; }
        public IReadOnlyList<SmsProviderPriceTier> Tiers { get; }
        public string DisplayName
        {
            get
            {
                // The config fallback in `MainWindow.SmsProvider.cs` builds a
                // choice whose English name *is* the raw id, because
                // `country_name` is optional in the config -- the backend
                // resolves a numeric country id through
                // `phone_proxy.COUNTRY_ID_TO_ISO` and never needs the label.
                // Rendering that as "6 (6)" reads like a bug, so collapse the
                // duplicate. Neither online catalog can hit this: there the
                // English name comes from the vendor's own field (`eng` for
                // sms-activate, `countryName` for nexsms).
                if (string.Equals(EnglishName, Id, StringComparison.Ordinal))
                {
                    return string.IsNullOrWhiteSpace(ChineseName) ? $"国家 {Id}" : $"{ChineseName} ({Id})";
                }
                return string.IsNullOrWhiteSpace(ChineseName)
                    ? $"{EnglishName} ({Id})"
                    : $"{ChineseName} / {EnglishName} ({Id})";
            }
        }
    }

    internal sealed class SmsProviderPriceTier
    {
        /// <summary>
        /// `Count` for a tier that came from the config rather than from a
        /// vendor price list, i.e. one whose inventory was never queried.
        ///
        /// It is a sentinel instead of `0` because `0` is a real answer -- an
        /// out-of-stock tier -- and the dialog renders the two differently.
        /// Both online catalogs produce real counts (sms-activate via
        /// `ParsePriceTiers`, nexsms via `ParseNexsmsPriceMap`); only the
        /// config fallback in `MainWindow.SmsProvider.cs` does not know the
        /// inventory and must not claim it does.
        /// </summary>
        internal const int UnknownCount = -1;

        internal SmsProviderPriceTier(string price, int count, string providerIds = "")
        {
            Price = price;
            Count = count;
            ProviderIds = providerIds ?? "";
            decimal.TryParse(price, NumberStyles.Number, CultureInfo.InvariantCulture, out decimal numericPrice);
            NumericPrice = numericPrice;
        }

        public string Price { get; }
        public int Count { get; }
        public string ProviderIds { get; }
        public decimal NumericPrice { get; }
        public string DisplayName => Count < 0
            ? $"${Price} / 个 · 库存未查询"
            : $"${Price} / 个 · 库存 {Count}";
    }
}
