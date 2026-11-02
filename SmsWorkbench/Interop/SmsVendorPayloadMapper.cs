// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Turns the backend's vendor-catalog payload into the dialog's view types.
    /// </summary>
    /// <remarks>
    /// <para>
    /// P7-B moved the vendor reads to `sms_tool` (`providers/sms_vendor_api.py`,
    /// exposed as the `provider-catalog` / `provider-balance` desktop-read ops), so
    /// the desktop no longer parses two vendor wire formats. This is the only place
    /// that knows the channel's shape:
    /// </para>
    /// <code>
    /// {"ok": true, "provider": "smsbower",
    ///  "countries": [{"id": "38", "name": "Indonesia", "name_zh": "印度尼西亚",
    ///                 "tiers": [{"price": "0.25", "count": 42}]}]}
    /// {"ok": true, "balance": "12.34"}
    /// </code>
    /// <para>
    /// Both readers are total: a missing, malformed or `ok: false` payload yields an
    /// empty catalog / empty balance, which is exactly the state the dialog already
    /// renders as "no online catalog" and "balance --".
    /// </para>
    /// </remarks>
    // internal: the view types it returns are internal to this assembly.
    internal static class SmsVendorPayloadMapper
    {
        /// <summary>Tiers with an unqueried inventory carry <see cref="SmsProviderPriceTier.UnknownCount"/>.</summary>
        public static IReadOnlyList<SmsProviderCountryChoice> ParseCatalog(JsonElement payload)
        {
            var countries = new List<SmsProviderCountryChoice>();
            if (payload.ValueKind != JsonValueKind.Object
                || !IsOk(payload)
                || !payload.TryGetProperty("countries", out JsonElement rows)
                || rows.ValueKind != JsonValueKind.Array)
            {
                return countries;
            }

            foreach (JsonElement row in rows.EnumerateArray())
            {
                if (row.ValueKind != JsonValueKind.Object) continue;
                string id = Text(row, "id");
                if (id.Length == 0) continue;

                var tiers = new List<SmsProviderPriceTier>();
                if (row.TryGetProperty("tiers", out JsonElement tierRows) && tierRows.ValueKind == JsonValueKind.Array)
                {
                    foreach (JsonElement tier in tierRows.EnumerateArray())
                    {
                        if (tier.ValueKind != JsonValueKind.Object) continue;
                        string price = Text(tier, "price");
                        if (price.Length == 0) continue;
                        tiers.Add(new SmsProviderPriceTier(price, Int(tier, "count")));
                    }
                }
                // A country the vendor cannot serve for this service is not an
                // option: the backend already drops them, this keeps the desktop
                // honest if a payload ever carries one.
                if (tiers.Count == 0) continue;

                countries.Add(new SmsProviderCountryChoice(id, Text(row, "name") is var name && name.Length > 0 ? name : id, Text(row, "name_zh"), tiers));
            }
            return countries;
        }

        /// <summary>The vendor balance, or "" when the read did not produce one.</summary>
        public static string ParseBalance(JsonElement payload)
        {
            if (payload.ValueKind != JsonValueKind.Object || !IsOk(payload)) return "";
            return Text(payload, "balance");
        }

        private static bool IsOk(JsonElement payload)
        {
            return !payload.TryGetProperty("ok", out JsonElement ok) || ok.ValueKind == JsonValueKind.True;
        }

        private static string Text(JsonElement element, string name)
        {
            if (!element.TryGetProperty(name, out JsonElement value)) return "";
            return value.ValueKind switch
            {
                JsonValueKind.String => value.GetString() ?? "",
                JsonValueKind.Number => value.ToString(),
                _ => "",
            };
        }

        private static int Int(JsonElement element, string name)
        {
            if (!element.TryGetProperty(name, out JsonElement value)) return SmsProviderPriceTier.UnknownCount;
            if (value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out int number)) return number;
            return int.TryParse(value.ToString(), out int parsed) ? parsed : SmsProviderPriceTier.UnknownCount;
        }
    }
}
