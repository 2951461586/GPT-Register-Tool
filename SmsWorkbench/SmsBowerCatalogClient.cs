namespace SmsWorkbench
{
    internal static class SmsBowerCatalogClient
    {
        internal const string DefaultEndpoint = "https://smsbower.page/stubs/handler_api.php";
        internal const string OpenAiService = "dr";

        internal static async Task<IReadOnlyList<SmsBowerCountryChoice>> LoadOpenAiCatalogAsync(
            HttpClient httpClient,
            string apiKey,
            string endpoint)
        {
            Task<string> countriesTask = GetTextAsync(httpClient, endpoint, apiKey, "getCountries");
            Task<string> pricesTask = GetTextAsync(httpClient, endpoint, apiKey, "getPricesV2", OpenAiService);
            await Task.WhenAll(countriesTask, pricesTask);

            var metadata = ParseCountries(await countriesTask);
            return ParsePriceTiers(await pricesTask, metadata);
        }

        private static Dictionary<string, SmsBowerCountryMetadata> ParseCountries(string json)
        {
            var metadata = new Dictionary<string, SmsBowerCountryMetadata>(StringComparer.OrdinalIgnoreCase);
            using JsonDocument document = JsonDocument.Parse(json);
            if (document.RootElement.ValueKind != JsonValueKind.Object) return metadata;

            foreach (JsonProperty property in document.RootElement.EnumerateObject())
            {
                JsonElement item = property.Value;
                string id = JsonString(item, "id", property.Name);
                metadata[id] = new SmsBowerCountryMetadata(
                    JsonString(item, "eng", id),
                    JsonString(item, "chn", ""));
            }
            return metadata;
        }

        private static IReadOnlyList<SmsBowerCountryChoice> ParsePriceTiers(
            string json,
            Dictionary<string, SmsBowerCountryMetadata> metadata)
        {
            var countries = new List<SmsBowerCountryChoice>();
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
                    .Select(ParseTier)
                    .Where(item => item != null && item.Count > 0)
                    .OrderBy(item => item.NumericPrice)
                    .ToList();
                if (tiers.Count == 0) continue;

                metadata.TryGetValue(countryProperty.Name, out SmsBowerCountryMetadata info);
                info ??= new SmsBowerCountryMetadata(countryProperty.Name, "");
                countries.Add(new SmsBowerCountryChoice(
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

        private static SmsBowerPriceTier ParseTier(JsonProperty property)
        {
            int count = JsonInteger(property.Value);
            if (count <= 0
                || !decimal.TryParse(property.Name, NumberStyles.Number, CultureInfo.InvariantCulture, out decimal price))
            {
                return null;
            }
            return new SmsBowerPriceTier(price.ToString("0.########", CultureInfo.InvariantCulture), count);
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

        private sealed record SmsBowerCountryMetadata(string EnglishName, string ChineseName);
    }

    internal sealed class SmsBowerCountryChoice
    {
        internal SmsBowerCountryChoice(
            string id,
            string englishName,
            string chineseName,
            IReadOnlyList<SmsBowerPriceTier> tiers)
        {
            Id = id;
            EnglishName = string.IsNullOrWhiteSpace(englishName) ? id : englishName;
            ChineseName = chineseName ?? "";
            Tiers = tiers;
        }

        public string Id { get; }
        public string EnglishName { get; }
        public string ChineseName { get; }
        public IReadOnlyList<SmsBowerPriceTier> Tiers { get; }
        public string DisplayName => string.IsNullOrWhiteSpace(ChineseName)
            ? $"{EnglishName} ({Id})"
            : $"{ChineseName} / {EnglishName} ({Id})";
    }

    internal sealed class SmsBowerPriceTier
    {
        internal SmsBowerPriceTier(string price, int count)
        {
            Price = price;
            Count = count;
            decimal.TryParse(price, NumberStyles.Number, CultureInfo.InvariantCulture, out decimal numericPrice);
            NumericPrice = numericPrice;
        }

        public string Price { get; }
        public int Count { get; }
        public decimal NumericPrice { get; }
        public string DisplayName => $"${Price} / 个 · 库存 {Count}";
    }
}
