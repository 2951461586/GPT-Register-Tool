using System.Text;
using System.Text.Json.Nodes;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class SettingsServiceTests
{
    private static readonly string[] ExpectedProxyOrder =
    {
        "http://primary",
        "http://secondary",
        "http://third"
    };

    [Fact]
    public void SavePreservesUnknownFieldsOrdersProxyPoolAndReplacesAtomically()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        File.WriteAllText(configPath, """
            {
              "unknown_extension": { "keep": 42 },
              "proxy": { "pool": ["http://old"] },
              "protocol_payments": {
                "matrix": { "cells": [] },
                "methods": { "blik": { "blik_code": "123456" } }
              }
            }
            """, new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));
        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();
        Field(categories, "registration_proxy").Value = "http://primary";
        Field(categories, "registration_proxy_pool").Value = "http://secondary\nhttp://primary\nHTTP://SECONDARY\nhttp://third";
        Field(categories, "protocol_payment_matrix").Value = "{\"cells\":[{\"name\":\"vn\"}]}";

        SettingsSaveResult result = service.Save(categories);

        Assert.True(result.Ok, result.Error);
        JsonObject root = JsonNode.Parse(File.ReadAllText(configPath, Encoding.UTF8))!.AsObject();
        Assert.Equal(42, root["unknown_extension"]!["keep"]!.GetValue<int>());
        Assert.Equal("http://primary", root["proxy"]!["registration"]!.GetValue<string>());
        Assert.Equal("http://primary", root["proxy"]!["default"]!.GetValue<string>());
        string[] proxyPool = root["proxy"]!["pool"]!.AsArray().Select(node => node!.GetValue<string>()).ToArray();
        Assert.Equal(ExpectedProxyOrder, proxyPool, StringComparer.OrdinalIgnoreCase);
        Assert.Null(root["protocol_payments"]!["methods"]!["blik"]!["blik_code"]);
        Assert.Empty(Directory.GetFiles(fixture.Path, "config.json.tmp.*"));
        byte[] bytes = File.ReadAllBytes(configPath);
        Assert.False(bytes.Length >= 3 && bytes[0] == 0xEF && bytes[1] == 0xBB && bytes[2] == 0xBF);
    }

    [Fact]
    public void InvalidMatrixDoesNotReplaceExistingConfig()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        const string original = "{\"preserve\":true,\"protocol_payments\":{\"matrix\":{\"cells\":[]}}}";
        File.WriteAllText(configPath, original, new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));
        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();
        Field(categories, "protocol_payment_matrix").Value = "{not-json";

        SettingsSaveResult result = service.Save(categories);

        Assert.False(result.Ok);
        Assert.Equal(original, File.ReadAllText(configPath, Encoding.UTF8));
        Assert.Empty(Directory.GetFiles(fixture.Path, "config.json.tmp.*"));
    }

    private static SettingFieldViewModel Field(
        IEnumerable<SettingsCategoryViewModel> categories,
        string key)
    {
        return categories
            .SelectMany(category => category.Sections)
            .SelectMany(section => section.Fields)
            .Single(field => field.Key == key);
    }
}
