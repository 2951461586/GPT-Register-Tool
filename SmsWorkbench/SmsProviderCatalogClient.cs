// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Reads a provider's OpenAI catalog and balance over the **wire**.
    ///
    /// <para>
    /// Two protocol families live here, one per section, because the dialog's
    /// two questions -- "what can I rent" and "what is my balance" -- have no
    /// common answer shape between them:
    /// </para>
    ///
    /// <list type="bullet">
    /// <item><description>
    /// **sm-activate handler** (`smsbower` / `herosms` / `grizzly`): one URL
    /// with `?action=`, and a balance that arrives as a bare
    /// `ACCESS_BALANCE:&lt;amount&gt;` line rather than as JSON.
    /// </description></item>
    /// <item><description>
    /// **nexsms REST** (`nexsms`): dated `/api/...` paths, the key in the query
    /// string as `apiKey`, and a `{code, message, data}` envelope on both.
    /// </description></item>
    /// </list>
    ///
    /// <para>
    /// The split is what the earlier single-family assumption got wrong:
    /// nexsms answers every `?action=` call with `403 Forbidden`, so selecting
    /// it and clicking 一键接码 dead-ended with no way forward. Which family a
    /// provider speaks comes from <see cref="SmsProviderCatalog"/>, whose table
    /// is pinned against `sms_tool/sms_providers.py` -- this file never decides
    /// it from the endpoint string.
    /// </para>
    /// </summary>
    internal static class SmsProviderCatalogClient
    {
        // The default host is NOT declared here: it depends on the selected
        // provider, and a second copy of it in this file is exactly what would
        // drift. See SmsProviderCatalog.
        internal const string OpenAiService = "dr";

        //: Wire names for an offer's unit price. `price` = smsbower / grizzly,
        //: `cost` = herosms. Measured 2026-09-23 -- see TryReadPrice.
        private static readonly string[] PriceFields = { "price", "cost" };

        // -- sms-activate handler ---------------------------------------------

        /// <summary>
        /// The country/tier catalog for one sms-activate vendor, over two
        /// concurrent reads: `getCountries` (id → eng/chn names) and the price
        /// list (`getPricesV3`, falling back to `getPrices`).
        /// </summary>
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

        // -- nexsms (`nexsms_json`) ------------------------------------------
        //
        // A different protocol family, so nothing above is reusable: plain REST
        // under `/api/`, the key in the query string as **`apiKey`** (camel
        // case, not `api_key`), and every reply wrapped in a
        // `{code, message, data}` envelope where only `code == 0` is success.
        //
        // The paths live here rather than in a shared table because they are a
        // property of this one vendor's API, and `sms_tool/nexsms.py` already
        // owns the same three strings. Two copies of a path is a drift risk,
        // but a *different* risk from the one that matters here: the parity
        // test that pins this file against Python (`test_settings_catalog_
        // provider_parity.py`) covers the provider table, not the paths, and
        // there is no compiler between the languages. Keeping the paths next
        // to their only caller makes a 404 traceable to one file.
        internal const string NexsmsBalancePath = "/api/balance";
        internal const string NexsmsCountriesPath = "/api/countries";
        internal const string NexsmsCountryByServicePath = "/api/getCountryByService";

        /// <summary>
        /// The nexsms country/tier catalog for one service.
        ///
        /// <para>
        /// 🔴 Two endpoints, and **both are needed**: `/api/countries` carries
        /// the Chinese display name but is keyed by id with **no English name
        /// at all**, while `/api/getCountryByService` carries `countryName`
        /// (English) plus the whole `priceMap`. Measured 2026-09-23:
        /// `/api/countries` returned 195 rows and `getCountryByService` 183 --
        /// so the intersection is what actually has numbers, and neither
        /// payload is redundant.
        /// </para>
        ///
        /// <para>
        /// The two calls run concurrently. They are independent, and the dialog
        /// is opened by a human waiting for it -- a serial round trip per
        /// endpoint would be paid on every open for no reason.
        /// </para>
        /// </summary>
        internal static async Task<IReadOnlyList<SmsProviderCountryChoice>> LoadNexsmsCatalogAsync(
            HttpClient httpClient,
            string apiKey,
            string endpoint,
            string service)
        {
            Task<string> countriesTask = GetNexsmsTextAsync(httpClient, endpoint, apiKey, NexsmsCountriesPath);
            Task<string> quotesTask = GetNexsmsTextAsync(
                httpClient, endpoint, apiKey, NexsmsCountryByServicePath, service);
            await Task.WhenAll(countriesTask, quotesTask);

            return ParseNexsmsCatalog(await countriesTask, await quotesTask);
        }

        /// <summary>
        /// Balance out of the `{code, message, data}` envelope.
        ///
        /// <para>
        /// The vendor reports `balance` as a **string** (`"0.2000"`), so it is
        /// read as text and returned verbatim: reformatting it to a decimal and
        /// back would turn a trailing-zero amount into a different-looking one,
        /// and `0.2000` already reads the way the vendor intends it to.
        /// </para>
        /// </summary>
        internal static async Task<string> LoadNexsmsBalanceAsync(
            HttpClient httpClient,
            string apiKey,
            string endpoint)
        {
            string body = await GetNexsmsTextAsync(httpClient, endpoint, apiKey, NexsmsBalancePath);
            JsonElement data = UnwrapNexsms(body, "balance lookup failed");
            string balance = JsonString(data, "balance", "").Trim();
            if (balance.Length == 0)
            {
                throw new InvalidDataException("nexsms balance reply carried no `balance` field");
            }
            return balance;
        }

        /// <summary>
        /// The two nexsms payloads in, the country/tier list out.
        ///
        /// <para>
        /// Split from the HTTP calls for the same reason
        /// <see cref="ParseCatalog"/> is: the shapes are what silently produced
        /// "no numbers" for the sms-activate vendors, and re-stating a shape
        /// through a mocked <see cref="HttpClient"/> would test the mock rather
        /// than the parser.
        /// </para>
        ///
        /// <para>
        /// A country is kept only when it appears in the **quote** payload:
        /// that is the one carrying `priceMap`, and a country with a name but
        /// no offers would render as an entry that rents nothing. The reverse
        /// is kept -- a quote without a Chinese name still rents -- because the
        /// vendor's two lists disagree by 12 rows in both directions
        /// (measured 2026-09-23).
        /// </para>
        /// </summary>
        internal static IReadOnlyList<SmsProviderCountryChoice> ParseNexsmsCatalog(
            string countriesJson,
            string quotesJson)
        {
            Dictionary<string, string> chineseNames = ParseNexsmsCountryNames(countriesJson);

            JsonElement quotes = UnwrapNexsms(quotesJson, "quote lookup failed");
            if (quotes.ValueKind != JsonValueKind.Array)
            {
                throw new InvalidDataException("nexsms 价格接口未返回国家列表");
            }

            var countries = new List<SmsProviderCountryChoice>();
            foreach (JsonElement quote in quotes.EnumerateArray())
            {
                string id = JsonString(quote, "countryId", "").Trim();
                if (id.Length == 0) continue;

                IReadOnlyList<SmsProviderPriceTier> tiers = ParseNexsmsPriceMap(quote);
                if (tiers.Count == 0) continue;

                chineseNames.TryGetValue(id, out string? chineseName);
                countries.Add(new SmsProviderCountryChoice(
                    id,
                    JsonString(quote, "countryName", id),
                    chineseName ?? "",
                    tiers));
            }

            return countries
                .OrderBy(item => item.EnglishName, StringComparer.OrdinalIgnoreCase)
                .ThenBy(item => item.Id, StringComparer.OrdinalIgnoreCase)
                .ToList();
        }

        /// <summary>`{id, name}` pairs, keyed by the id as text.</summary>
        private static Dictionary<string, string> ParseNexsmsCountryNames(string json)
        {
            var names = new Dictionary<string, string>(StringComparer.Ordinal);
            JsonElement data = UnwrapNexsms(json, "country lookup failed");
            if (data.ValueKind != JsonValueKind.Array) return names;

            foreach (JsonElement item in data.EnumerateArray())
            {
                string id = JsonString(item, "id", "").Trim();
                if (id.Length == 0) continue;
                names[id] = JsonString(item, "name", "").Trim();
            }
            return names;
        }

        /// <summary>
        /// One country's `priceMap` as tiers.
        ///
        /// <para>
        /// 🔴 The map is **price → stock**, not a list: `{"0.1207": 56174,
        /// "0.1428": 64579, ...}`. Measured 2026-09-23 it arrives already
        /// ascending by price, but the order is not relied on -- it is sorted
        /// here, because `SmsProviderPriceTier.NumericPrice` exists precisely so
        /// the dialog can trust the order rather than the payload's.
        /// </para>
        ///
        /// <para>
        /// Prices are **strings** on the wire. They are passed through verbatim
        /// rather than round-tripped through `decimal`, so the price written
        /// back into the config is byte-identical to the one the vendor quoted
        /// -- the backend compares that string against its own quote, and
        /// `"0.1207"` must not become `"0.12070"` on the way.
        /// </para>
        /// </summary>
        private static IReadOnlyList<SmsProviderPriceTier> ParseNexsmsPriceMap(JsonElement quote)
        {
            var tiers = new List<SmsProviderPriceTier>();
            if (!quote.TryGetProperty("priceMap", out JsonElement priceMap)
                || priceMap.ValueKind != JsonValueKind.Object)
            {
                return tiers;
            }

            foreach (JsonProperty entry in priceMap.EnumerateObject())
            {
                if (!decimal.TryParse(entry.Name, NumberStyles.Number, CultureInfo.InvariantCulture,
                                      out decimal price))
                {
                    continue;
                }
                int count = JsonInteger(entry.Value);
                if (count <= 0) continue;
                tiers.Add(new SmsProviderPriceTier(
                    price.ToString("0.########", CultureInfo.InvariantCulture), count));
            }

            return tiers.OrderBy(item => item.NumericPrice).ToList();
        }

        /// <summary>
        /// Check `code == 0` and hand back `data`.
        ///
        /// <para>
        /// 🔴 `code` is compared **numerically**, so a vendor that sends `"0"`
        /// instead of `0` is not read as a failure. The Python client makes the
        /// same concession for the same reason
        /// (`sms_tool/nexsms.py::_unwrap`), and the two must agree: a reply the
        /// CLI accepts is a reply the dialog must accept.
        /// </para>
        /// </summary>
        private static JsonElement UnwrapNexsms(string json, string what)
        {
            using JsonDocument document = JsonDocument.Parse(json);
            JsonElement root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                throw new InvalidDataException(what + ": unexpected reply " + Clip(json));
            }

            if (!root.TryGetProperty("code", out JsonElement codeElement)
                || !decimal.TryParse(codeElement.ToString(), NumberStyles.Number,
                                     CultureInfo.InvariantCulture, out decimal code)
                || code != 0m)
            {
                string message = JsonString(root, "message", "").Trim();
                throw new InvalidDataException(
                    what + ": nexsms code=" + codeElement.ToString()
                    + (message.Length > 0 ? " (" + message + ")" : ""));
            }

            // Cloned: the document is disposed when this method returns, and the
            // caller still needs the element.
            return root.TryGetProperty("data", out JsonElement data)
                ? data.Clone()
                : default;
        }

        /// <summary>
        /// One GET against a nexsms `/api/` path.
        ///
        /// <para>
        /// 🔴 The path is appended to the endpoint's **path part**, not to the
        /// whole string. A configured endpoint may carry its own query string
        /// (`https://host?tenant=1`), and appending `/api/countries` after that
        /// query would fold the path into the query value -- the request would
        /// leave with the right host and the wrong route, and the vendor's 404
        /// would read like a wrong path rather than a malformed URL. The
        /// sms-activate reader never had to care: there the endpoint *is* the
        /// handler and every parameter is a query argument.
        /// </para>
        /// </summary>
        private static async Task<string> GetNexsmsTextAsync(
            HttpClient httpClient,
            string endpoint,
            string apiKey,
            string path,
            string service = "")
        {
            string trimmed = (endpoint ?? "").Trim().TrimEnd('/');
            int question = trimmed.IndexOf('?');
            string baseUrl = question < 0 ? trimmed : trimmed[..question];
            string existingQuery = question < 0 ? "" : trimmed[(question + 1)..];

            string query = "apiKey=" + Uri.EscapeDataString(apiKey);
            if (!string.IsNullOrWhiteSpace(service))
            {
                query += "&serviceCode=" + Uri.EscapeDataString(service);
            }
            if (existingQuery.Length > 0)
            {
                query = existingQuery + "&" + query;
            }

            string url = baseUrl + path + "?" + query;
            using HttpResponseMessage response = await httpClient.GetAsync(url);
            string body = (await response.Content.ReadAsStringAsync()).Trim();
            response.EnsureSuccessStatusCode();
            if (!body.StartsWith("{", StringComparison.Ordinal))
            {
                throw new InvalidDataException(Clip(body));
            }
            return body;
        }

        private static string Clip(string text)
        {
            return text.Length > 160 ? text[..160] : text;
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
                throw new InvalidDataException(Clip(body));
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
                throw new InvalidDataException(Clip(body));
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
