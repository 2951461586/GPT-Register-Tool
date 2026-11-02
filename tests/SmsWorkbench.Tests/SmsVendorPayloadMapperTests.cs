// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using System.Text.Json;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

/// <summary>
/// The channel payload -> dialog view types (P7-B). The backend owns the vendor wire
/// formats; this mapper owns the one shape the channel promises.
/// </summary>
public sealed class SmsVendorPayloadMapperTests
{
    private static JsonElement Json(string text)
    {
        using JsonDocument document = JsonDocument.Parse(text);
        return document.RootElement.Clone();
    }

    [Fact]
    public void ParseCatalog_MapsCountriesAndTiers()
    {
        string payload = """
            {"ok": true, "provider": "smsbower",
             "countries": [
               {"id": "38", "name": "Indonesia", "name_zh": "印度尼西亚",
                "tiers": [{"price": "0.25", "count": 42}, {"price": "0.30", "count": 0}]},
               {"id": "16", "name": "", "name_zh": "", "tiers": [{"price": "0.10", "count": 7}]}
             ]}
            """;

        var countries = SmsVendorPayloadMapper.ParseCatalog(Json(payload));

        Assert.Equal(2, countries.Count);
        Assert.Equal("38", countries[0].Id);
        Assert.Equal("Indonesia", countries[0].EnglishName);
        Assert.Equal("印度尼西亚", countries[0].ChineseName);
        Assert.Equal(new[] { "0.25", "0.30" }, countries[0].Tiers.Select(tier => tier.Price));
        Assert.Equal(new[] { 42, 0 }, countries[0].Tiers.Select(tier => tier.Count));
        // An empty vendor name falls back to the id, never to an empty label.
        Assert.Equal("16", countries[1].EnglishName);
    }

    [Fact]
    public void ParseCatalog_DropsTierlessCountriesAndToleratesJunk()
    {
        string payload = """
            {"ok": true, "countries": [
               {"id": "38", "name": "Indonesia", "tiers": []},
               {"name": "no id", "tiers": [{"price": "1", "count": 1}]},
               {"id": "16", "name": "Malaysia", "tiers": [{"price": ""}]},
               {"id": "7", "name": "Russia", "tiers": [{"price": "0.20", "count": 3}]}
             ]}
            """;

        var countries = SmsVendorPayloadMapper.ParseCatalog(Json(payload));

        Assert.Single(countries);
        Assert.Equal("7", countries[0].Id);
    }

    [Fact]
    public void ParseCatalog_ReturnsEmptyWhenTheReadFailedOrShapeDrifted()
    {
        Assert.Empty(SmsVendorPayloadMapper.ParseCatalog(Json("""{"ok": false, "error": "transport:reset"}""")));
        Assert.Empty(SmsVendorPayloadMapper.ParseCatalog(Json("""{"ok": true}""")));
        Assert.Empty(SmsVendorPayloadMapper.ParseCatalog(Json("""[]""")));
    }

    [Fact]
    public void ParseBalance_ReadsTheValueAndDegradesToEmpty()
    {
        Assert.Equal("12.34", SmsVendorPayloadMapper.ParseBalance(Json("""{"ok": true, "balance": "12.34"}""")));
        Assert.Equal("", SmsVendorPayloadMapper.ParseBalance(Json("""{"ok": false, "error": "balance_missing"}""")));
        Assert.Equal("", SmsVendorPayloadMapper.ParseBalance(Json("""{"ok": true}""")));
    }
}
