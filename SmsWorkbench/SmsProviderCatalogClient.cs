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

        internal static async Task<IReadOnlyList<SmsProviderCountryChoice>> LoadOpenAiCatalogAsync(
            HttpClient httpClient,
            string apiKey,
            string endpoint)
        {
            Task<string> countriesTask = GetTextAsync(httpClient, endpoint, apiKey, "getCountries");
            Task<string> pricesTask = GetTextAsync(httpClient, endpoint, apiKey, "getPricesV3", OpenAiService);
            await Task.WhenAll(countriesTask, pricesTask);

            var metadata = ParseCountries(await countriesTask);
            return ParsePriceTiers(await pricesTask, metadata);
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

                var tiers = service.EnumerateObject()
                    .Select(ParseOffer)
                    .Where(item => item != null && item.Count > 0)
                .GroupBy(item => item!.Price)
                .Select(group => new SmsProviderPriceTier(
                    group.Key.ToString("0.########", CultureInfo.InvariantCulture),
                    group.Sum(item => item!.Count),
                    string.Join(",", group.Select(item => item!.ProviderId).Where(value => value.Length > 0).Distinct())))
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

        private static SmsProviderOffer? ParseOffer(JsonProperty property)
        {
            string priceText = property.Value.ValueKind == JsonValueKind.Object
                ? JsonString(property.Value, "price", "")
                : property.Name;
            int count = property.Value.ValueKind == JsonValueKind.Object
                && property.Value.TryGetProperty("count", out JsonElement countElement)
                    ? JsonInteger(countElement)
                    : JsonInteger(property.Value);
            string providerId = property.Value.ValueKind == JsonValueKind.Object
                ? JsonString(property.Value, "provider_id", property.Name)
                : "";
            if (count <= 0
                || !decimal.TryParse(priceText, NumberStyles.Number, CultureInfo.InvariantCulture, out decimal price))
            {
                return null;
            }
            return new SmsProviderOffer(price, count, providerId);
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
        public string DisplayName => string.IsNullOrWhiteSpace(ChineseName)
            ? $"{EnglishName} ({Id})"
            : $"{ChineseName} / {EnglishName} ({Id})";
    }

    internal sealed class SmsProviderPriceTier
    {
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
        public string DisplayName => $"${Price} / 个 · 库存 {Count}";
    }
}
