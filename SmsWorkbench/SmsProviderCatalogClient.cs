// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    internal static class SmsProviderCatalogClient
    {
        // The default host is NOT declared here: it depends on the selected
        // provider, and a second copy of it in this file is exactly what would
        // drift. See SmsProviderCatalog.
        internal const string OpenAiService = "dr";

        //: Wire names for an offer's unit price. `price` = smsbower / grizzly,
        //: `cost` = herosms. Measured 2026-09-23 -- see TryReadPrice.
        private static readonly string[] PriceFields = { "price", "cost" };

        internal static async Task<IReadOnlyList<SmsProviderCountryChoice>> LoadOpenAiCatalogAsync(
            HttpClient httpClient,
            string apiKey,
            string endpoint)
        {
            Task<string> countriesTask = GetTextAsync(httpClient, endpoint, apiKey, "getCountries");
            Task<string> pricesTask = LoadPricesAsync(httpClient, endpoint, apiKey);
            await Task.WhenAll(countriesTask, pricesTask);

            return ParseCatalog(await countriesTask, await pricesTask);
        }

        /// <summary>
        /// The two catalog payloads in, the country/tier list out.
        ///
        /// Split out of the HTTP call purely so it can be tested: the shapes below
        /// are the part that silently produced "no numbers" for two of three
        /// vendors, and none of that is reachable through a mocked
        /// <see cref="HttpClient"/> without also re-stating the shape being
        /// tested.
        /// </summary>
        internal static IReadOnlyList<SmsProviderCountryChoice> ParseCatalog(
            string countriesJson,
            string pricesJson)
        {
            return ParsePriceTiers(pricesJson, ParseCountries(countriesJson));
        }

        /// <summary>
        /// The OpenAI price list, trying the newer action first.
        ///
        /// <para>
        /// 🔴 `getPricesV3` is not universal even inside the sms-activate family:
        /// smsbower and grizzly answer it, **herosms returns `404`** and serves
        /// only the older `getPrices`. Deciding this from the endpoint's answer
        /// rather than from a per-provider table is deliberate -- a table would
        /// have to be re-verified against every vendor's docs forever, and the
        /// failure mode of a stale entry is the worst kind: a dialog that looks
        /// fine and reports "no numbers".
        /// </para>
        /// </summary>
        private static async Task<string> LoadPricesAsync(
            HttpClient httpClient,
            string endpoint,
            string apiKey)
        {
            try
            {
                return await GetTextAsync(httpClient, endpoint, apiKey, "getPricesV3", OpenAiService);
            }
            catch (Exception v3Error) when (v3Error is HttpRequestException or InvalidDataException)
            {
                try
                {
                    return await GetTextAsync(httpClient, endpoint, apiKey, "getPrices", OpenAiService);
                }
                catch (Exception legacyError)
                {
                    throw new InvalidDataException(
                        "价格接口不可用：getPricesV3 与 getPrices 都失败"
                        + $"（V3: {v3Error.Message} / 旧版: {legacyError.Message}）",
                        legacyError);
                }
            }
        }

        internal static async Task<string> LoadBalanceAsync(HttpClient httpClient, string apiKey, string endpoint)
        {
            string separator = endpoint.Contains('?') ? "&" : "?";
            string url = endpoint + separator
                + "api_key=" + Uri.EscapeDataString(apiKey)
                + "&action=getBalance";
            using HttpResponseMessage response = await httpClient.GetAsync(url);
            string body = (await response.Content.ReadAsStringAsync()).Trim();
            response.EnsureSuccessStatusCode();
            const string prefix = "ACCESS_BALANCE:";
            if (!body.StartsWith(prefix, StringComparison.Ordinal))
            {
                throw new InvalidDataException(body.Length > 160 ? body[..160] : body);
            }
            return body[prefix.Length..].Trim();
        }

        private static Dictionary<string, SmsProviderCountryMetadata> ParseCountries(string json)
        {
            var metadata = new Dictionary<string, SmsProviderCountryMetadata>(StringComparer.OrdinalIgnoreCase);
            using JsonDocument document = JsonDocument.Parse(json);
            if (document.RootElement.ValueKind != JsonValueKind.Object) return metadata;

            foreach (JsonProperty property in document.RootElement.EnumerateObject())
            {
                JsonElement item = property.Value;
                string id = JsonString(item, "id", property.Name);
                metadata[id] = new SmsProviderCountryMetadata(
                    JsonString(item, "eng", id),
                    JsonString(item, "chn", ""));
            }
            return metadata;
        }

        private static IReadOnlyList<SmsProviderCountryChoice> ParsePriceTiers(
            string json,
            Dictionary<string, SmsProviderCountryMetadata> metadata)
        {
            var countries = new List<SmsProviderCountryChoice>();
            using JsonDocument document = JsonDocument.Parse(json);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
            {
                throw new InvalidDataException("价格接口未返回国家列表");
            }

            foreach (JsonProperty countryProperty in document.RootElement.EnumerateObject())
            {
                if (!countryProperty.Value.TryGetProperty(OpenAiService, out JsonElement service)
                    || service.ValueKind != JsonValueKind.Object)
                {
                    continue;
                }

                var tiers = ParseOffers(service)
                    .Where(item => item.Count > 0)
                    .GroupBy(item => item.Price)
                    .Select(group => new SmsProviderPriceTier(
                        group.Key.ToString("0.########", CultureInfo.InvariantCulture),
                        group.Sum(item => item.Count),
                        string.Join(",", group.Select(item => item.ProviderId).Where(value => value.Length > 0).Distinct())))
                    .OrderBy(item => item.NumericPrice)
                    .ToList();
                if (tiers.Count == 0) continue;

                metadata.TryGetValue(countryProperty.Name, out SmsProviderCountryMetadata? info);
                info ??= new SmsProviderCountryMetadata(countryProperty.Name, "");
                countries.Add(new SmsProviderCountryChoice(
                    countryProperty.Name,
                    info.EnglishName,
                    info.ChineseName,
                    tiers));
            }

            return countries
                .OrderBy(item => item.EnglishName, StringComparer.OrdinalIgnoreCase)
                .ThenBy(item => item.Id, StringComparer.OrdinalIgnoreCase)
                .ToList();
        }

        /// <summary>
        /// Every offer under one country's `dr` node.
        ///
        /// <para>
        /// 🔴 There are two nestings in the wild, and the three sms-activate
        /// vendors disagree about which they use:
        /// </para>
        /// <list type="bullet">
        /// <item><description>
        /// nested -- <c>country → service → provider_id → {price, count, provider_id}</c>
        /// (smsbower, measured 2026-09-23)
        /// </description></item>
        /// <item><description>
        /// flat -- <c>country → service → {price|cost, count}</c>
        /// (grizzly uses `price`, herosms uses `cost`; both measured the same day)
        /// </description></item>
        /// </list>
        /// <para>
        /// The shape is read from the payload rather than from a per-provider
        /// table: a table is one more thing that goes stale silently, and getting
        /// it wrong produces a dialog that reports "no numbers" for a vendor that
        /// has thousands. Note the earlier code assumed the nested shape only,
        /// which is why grizzly's 9310 numbers and herosms' 617597 both showed up
        /// as "当前没有可用的 OpenAI 号码".
        /// </para>
        /// </summary>
        private static IReadOnlyList<SmsProviderOffer> ParseOffers(JsonElement service)
        {
            if (service.ValueKind != JsonValueKind.Object) return Array.Empty<SmsProviderOffer>();

            // Flat: the node itself carries the price.
            if (TryReadOffer(service, "", out SmsProviderOffer flat))
            {
                return new[] { flat };
            }

            var offers = new List<SmsProviderOffer>();
            foreach (JsonProperty child in service.EnumerateObject())
            {
                if (TryReadOffer(child.Value, child.Name, out SmsProviderOffer nested))
                {
                    offers.Add(nested);
                }
            }
            if (offers.Count > 0) return offers;

            // Legacy last resort: `{"0.054": 12}` -- price as the key, count as the
            // value. No observed vendor answers this today, but the earlier parser
            // accepted it and dropping that silently would turn a working path
            // into an empty list.
            foreach (JsonProperty child in service.EnumerateObject())
            {
                if (!decimal.TryParse(child.Name, NumberStyles.Number, CultureInfo.InvariantCulture,
                                      out decimal price))
                {
                    continue;
                }
                int count = JsonInteger(child.Value);
                if (count > 0) offers.Add(new SmsProviderOffer(price, count, ""));
            }
            return offers;
        }

        private static bool TryReadOffer(JsonElement node, string fallbackProviderId, out SmsProviderOffer offer)
        {
            offer = null!;
            if (node.ValueKind != JsonValueKind.Object) return false;
            if (!TryReadPrice(node, out decimal price)) return false;
            if (!node.TryGetProperty("count", out JsonElement countElement)) return false;
            int count = JsonInteger(countElement);
            if (count <= 0) return false;
            offer = new SmsProviderOffer(price, count, JsonString(node, "provider_id", fallbackProviderId));
            return true;
        }

        /// <summary>
        /// The offer's unit price.
        ///
        /// <para>
        /// `price` is what smsbower and grizzly report; herosms reports the same
        /// field as `cost`. Read whichever is present instead of switching on the
        /// provider -- the wire field is a property of the payload, and the
        /// provider is not available here anyway.
        /// </para>
        /// </summary>
        private static bool TryReadPrice(JsonElement node, out decimal price)
        {
            foreach (string field in PriceFields)
            {
                if (!node.TryGetProperty(field, out JsonElement element)) continue;
                string text = element.ValueKind == JsonValueKind.String
                    ? element.GetString() ?? ""
                    : element.ToString();
                if (decimal.TryParse(text, NumberStyles.Number, CultureInfo.InvariantCulture, out price))
                {
                    return true;
                }
            }
            price = 0m;
            return false;
        }

        private static async Task<string> GetTextAsync(
            HttpClient httpClient,
            string endpoint,
            string apiKey,
            string action,
            string service = "")
        {
            string separator = endpoint.Contains('?') ? "&" : "?";
            string url = endpoint + separator
                + "api_key=" + Uri.EscapeDataString(apiKey)
                + "&action=" + Uri.EscapeDataString(action);
            if (!string.IsNullOrWhiteSpace(service))
            {
                url += "&service=" + Uri.EscapeDataString(service);
            }

            using HttpResponseMessage response = await httpClient.GetAsync(url);
            string body = (await response.Content.ReadAsStringAsync()).Trim();
            response.EnsureSuccessStatusCode();
            if (!body.StartsWith("{", StringComparison.Ordinal))
            {
                throw new InvalidDataException(body.Length > 160 ? body[..160] : body);
            }
            return body;
        }

        private static string JsonString(JsonElement element, string name, string fallback)
        {
            if (element.ValueKind == JsonValueKind.Object && element.TryGetProperty(name, out JsonElement value))
            {
                return value.ValueKind == JsonValueKind.String ? value.GetString() ?? fallback : value.ToString();
            }
            return fallback;
        }

        private static int JsonInteger(JsonElement element)
        {
            if (element.ValueKind == JsonValueKind.Number && element.TryGetInt32(out int number)) return number;
            return int.TryParse(element.ToString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out number) ? number : 0;
        }

        private sealed record SmsProviderCountryMetadata(string EnglishName, string ChineseName);
        private sealed record SmsProviderOffer(decimal Price, int Count, string ProviderId);
    }

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
                // The non-sms-activate fallback in `MainWindow.SmsProvider.cs`
                // builds a choice whose English name *is* the raw id, because
                // `country_name` is optional in the config -- the backend
                // resolves a numeric country id through
                // `phone_proxy.COUNTRY_ID_TO_ISO` and never needs the label.
                // Rendering that as "6 (6)" reads like a bug, so collapse the
                // duplicate. The sms-activate path can't hit this: there the
                // English name comes from the vendor's own `eng` field.
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
        /// out-of-stock tier -- and the dialog renders the two differently. Only
        /// the sms-activate catalog produces real counts
        /// (<see cref="SmsProviderCatalogClient.ParsePriceTiers"/>); the
        /// non-sms-activate fallback path in `MainWindow.SmsProvider.cs` does
        /// not know the inventory and must not claim it does.
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
