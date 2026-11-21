using SmsWorkbench.Localization;

namespace SmsWorkbench.Services
{
    public static class PaymentProxyRegionPlanner
    {
        private static readonly string[] PoolTextSeparators = { "\r\n", "\n", ",", ";" };
        private const string PoolLineSeparator = ProxyInputNormalizer.LineSeparator;

        public static string InferProxyRegion(string proxy)
        {
            string text = (proxy ?? "").Trim();
            if (text.Length == 0) return "";
            string username = "";
            int at = text.IndexOf('@');
            if (at >= 0)
            {
                string auth = text.Substring(0, at);
                int schemeSep = auth.IndexOf("://", StringComparison.Ordinal);
                if (schemeSep >= 0) auth = auth.Substring(schemeSep + 3);
                int colon = auth.IndexOf(':');
                username = colon >= 0 ? auth.Substring(0, colon) : auth;
            }
            else
            {
                string[] parts = text.Split(':');
                if (parts.Length >= 4) username = parts[2];
            }
            try { username = Uri.UnescapeDataString(username); } catch { }
            Match match = Regex.Match(username, @"(?:^|[_-])custom[_-]zone[_-]([A-Za-z]{2})(?=$|[_-])", RegexOptions.IgnoreCase);
            if (match.Success) return match.Groups[1].Value.ToUpperInvariant();
            Match cliproxy = Regex.Match(username, @"region-([A-Za-z]{2})(?=$|[-_:])", RegexOptions.IgnoreCase);
            if (cliproxy.Success) return cliproxy.Groups[1].Value.ToUpperInvariant();
            return "";
        }

        public static string[] SplitPoolText(string value)
            => (value ?? "")
                .Split(PoolTextSeparators, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

        public static void InitializeBuckets(
            string text, Dictionary<string, List<string>> buckets, List<string> order)
        {
            buckets.Clear();
            order.Clear();
            foreach (string line in SplitPoolText(text))
            {
                string region = InferProxyRegion(line);
                if (!buckets.TryGetValue(region, out List<string>? list))
                {
                    list = new List<string>();
                    buckets[region] = list;
                    order.Add(region);
                }
                list.Add(line);
            }
        }

        public static void SyncDisplay(
            string display, string country, Dictionary<string, List<string>> buckets, List<string> order)
        {
            if (string.IsNullOrWhiteSpace(country))
            {
                InitializeBuckets(display, buckets, order);
                return;
            }
            if (!buckets.TryGetValue(country, out List<string>? countryList) || countryList == null)
            {
                countryList = new List<string>();
                buckets[country] = countryList;
                if (order.FindIndex(item => string.Equals(item, country, StringComparison.OrdinalIgnoreCase)) < 0)
                    order.Add(country);
            }
            else
            {
                countryList.Clear();
            }
            if (!buckets.TryGetValue("", out List<string>? unknownList) || unknownList == null)
            {
                unknownList = new List<string>();
                buckets[""] = unknownList;
                if (order.FindIndex(item => item.Length == 0) < 0)
                    order.Add("");
            }
            else
            {
                unknownList.Clear();
            }
            foreach (string line in SplitPoolText(display))
            {
                string region = InferProxyRegion(line);
                if (string.Equals(region, country, StringComparison.OrdinalIgnoreCase))
                    countryList.Add(line);
                else
                    unknownList.Add(line);
            }
        }

        public static string ComputeDisplay(
            string country, Dictionary<string, List<string>> buckets, List<string> order)
        {
            var lines = new List<string>();
            if (string.IsNullOrWhiteSpace(country))
            {
                foreach (string region in order)
                    lines.AddRange(buckets[region]);
            }
            else
            {
                if (buckets.TryGetValue(country, out List<string>? countryList))
                    lines.AddRange(countryList);
                if (buckets.TryGetValue("", out List<string>? unknownList))
                    lines.AddRange(unknownList);
            }
            return string.Join(PoolLineSeparator, lines);
        }

        public static string FullPool(Dictionary<string, List<string>> buckets, List<string> order)
            => string.Join(PoolLineSeparator, order.SelectMany(region => buckets[region]));

        public static string FormatRegionSummary(Dictionary<string, List<string>> buckets, List<string> order)
        {
            if (buckets.Count == 0) return Loc.T("（空）");
            return string.Join(" · ", order.Select(region =>
                (region.Length == 0 ? Loc.T("未知") : region) + "×" + buckets[region].Count));
        }
    }
}
