using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class PaymentBatchServiceTests
{
    [Fact]
    public async Task ProbeModeUsesContractArgumentsAndAcceptsPayloadFromNonZeroExit()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), "{}");
        string emailFile = "";
        string matrixFile = "";
        var backend = new StubBackendClient
        {
            Handler = command =>
            {
                emailFile = ArgumentAfter(command.Arguments, "--email-file");
                matrixFile = ArgumentAfter(command.Arguments, "--payment-matrix");
                Assert.True(File.Exists(emailFile));
                Assert.True(File.Exists(matrixFile));
                return new BackendCommandResult(
                    3,
                    "",
                    "backend classified the run",
                    JsonElementOf("{\"ok\":false,\"error\":\"classified\"}"),
                    false);
            }
        };
        var service = new PaymentBatchService(new TestApplicationPaths(fixture.Path), backend);
        var request = new PaymentBatchRequest(
            new[] { new PaymentBatchAccount("first@example.com", true) },
            "momo",
            2,
            1,
            0,
            "probe-batch",
            "http://checkout-one\nhttp://checkout-two",
            "http://approve-jp",
            "ID",
            "JP",
            true,
            true,
            true,
            new[] { service.CreateDefaultMatrixRow("momo") });

        JsonElement payload = await service.RunAsync(request, CancellationToken.None);

        Assert.False(payload.GetProperty("ok").GetBoolean());
        Assert.NotNull(backend.LastCommand);
        Assert.Contains("--desktop-ipc", backend.LastCommand.Arguments);
        Assert.Contains("--extract-payment-link", backend.LastCommand.Arguments);
        Assert.Equal("momo", ArgumentAfter(backend.LastCommand.Arguments, "--payment-method"));
        Assert.Contains("--payment-probe-only", backend.LastCommand.Arguments);
        Assert.DoesNotContain("--no-require-zero", backend.LastCommand.Arguments);
        Assert.Equal("probe-batch", ArgumentAfter(backend.LastCommand.Arguments, "--payment-batch-id"));
        Assert.Equal("http://checkout-one" + Environment.NewLine + "http://checkout-two", ArgumentAfter(backend.LastCommand.Arguments, "--checkout-proxy-pool"));
        Assert.Equal("http://approve-jp", ArgumentAfter(backend.LastCommand.Arguments, "--approve-proxy-pool"));
        Assert.Equal("ID", ArgumentAfter(backend.LastCommand.Arguments, "--checkout-proxy-country"));
        Assert.Equal("JP", ArgumentAfter(backend.LastCommand.Arguments, "--approve-proxy-country"));
        Assert.Equal("JP", ArgumentAfter(backend.LastCommand.Arguments, "--update-proxy-country"));
        Assert.DoesNotContain("--auto-proxy-country", backend.LastCommand.Arguments);
        Assert.False(File.Exists(emailFile));
        Assert.False(File.Exists(matrixFile));
    }

    [Fact]
    public async Task ProxyProbeUsesSelectedCountryForApproveAndSharedUpdatePool()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), "{}");
        var backend = new StubBackendClient
        {
            Handler = _ => new BackendCommandResult(
                0,
                "",
                "",
                JsonElementOf("{\"ok\":true,\"stages\":{}}"),
                false)
        };
        var service = new PaymentBatchService(new TestApplicationPaths(fixture.Path), backend);

        await service.ProbeProxiesAsync(
            "momo",
            "http://checkout.example",
            "http://approve.example",
            "VN",
            "TR",
            CancellationToken.None);

        Assert.NotNull(backend.LastCommand);
        Assert.Equal("TR", ArgumentAfter(backend.LastCommand!.Arguments, "--approve-proxy-country"));
        Assert.Equal("TR", ArgumentAfter(backend.LastCommand.Arguments, "--update-proxy-country"));
        Assert.DoesNotContain("--auto-proxy-country", backend.LastCommand.Arguments);
    }

    [Fact]
    public async Task FormalModeSerializesValidatedMatrixAndOptions()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), "{}");
        string matrixJson = "";
        var backend = new StubBackendClient
        {
            Handler = command =>
            {
                matrixJson = File.ReadAllText(ArgumentAfter(command.Arguments, "--payment-matrix"));
                return new BackendCommandResult(0, "", "", JsonElementOf("{\"ok\":true}"), false);
            }
        };
        var service = new PaymentBatchService(new TestApplicationPaths(fixture.Path), backend);
        var matrix = new PaymentMatrixRow
        {
            Name = "vn-primary",
            RegistrationCountry = "vn",
            CheckoutCountry = "jp",
            SampleSize = 3
        };
        var request = new PaymentBatchRequest(
            new[] { new PaymentBatchAccount("first@example.com", false) },
            "momo",
            1,
            0,
            1,
            "formal-batch",
            "",
            "",
            "",
            "JP",
            false,
            false,
            false,
            new[] { matrix });

        await service.RunAsync(request, CancellationToken.None);

        Assert.NotNull(backend.LastCommand);
        Assert.DoesNotContain("--payment-probe-only", backend.LastCommand.Arguments);
        Assert.Contains("--no-jit-at-refresh", backend.LastCommand.Arguments);
        Assert.Contains("--no-require-zero", backend.LastCommand.Arguments);
        Assert.Equal("1", ArgumentAfter(backend.LastCommand.Arguments, "--payment-canary"));
        using JsonDocument document = JsonDocument.Parse(matrixJson);
        JsonElement cell = document.RootElement.GetProperty("cells")[0];
        Assert.Equal("VN", cell.GetProperty("registration_country").GetString());
        Assert.Equal("JP", cell.GetProperty("checkout_country").GetString());
        Assert.Equal(3, cell.GetProperty("sample_size").GetInt32());
    }

    [Fact]
    public async Task ManualAccessTokensUsePrivateMapFileWithoutLeakingIntoArguments()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), "{}");
        const string secret = "secret-manual-access-token";
        string tokenFile = "";
        var backend = new StubBackendClient
        {
            Handler = command =>
            {
                tokenFile = ArgumentAfter(command.Arguments, "--payment-token-map");
                Assert.True(File.Exists(tokenFile));
                string tokenJson = File.ReadAllText(tokenFile);
                Assert.Contains(secret, tokenJson);
                Assert.DoesNotContain(secret, command.Arguments);
                return new BackendCommandResult(0, "", "", JsonElementOf("{\"ok\":true}"), false);
            }
        };
        var service = new PaymentBatchService(new TestApplicationPaths(fixture.Path), backend);
        var request = new PaymentBatchRequest(
            new[] { new PaymentBatchAccount("AT-1", true, secret) },
            "momo", 1, 3, 0, "manual-at", "", "", "", "JP",
            true, true, true,
            new[] { service.CreateDefaultMatrixRow("momo") });

        await service.RunAsync(request, CancellationToken.None);

        Assert.NotEmpty(tokenFile);
        Assert.False(File.Exists(tokenFile));
        Assert.Equal("3", ArgumentAfter(backend.LastCommand!.Arguments, "--payment-retries"));
    }

    [Theory]
    [InlineData("gopay", "ID", "TH", "JP")]
    [InlineData("gcash", "PH", "PH", "PH")]
    [InlineData("grabpay", "PH", "PH", "PH")]
    public void WalletDefaultMatrixUsesMethodSpecificPromotionCountry(
        string paymentMethod,
        string expectedCountry,
        string expectedPromotionCountry,
        string expectedApproveCountry)
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), "{}");
        var service = new PaymentBatchService(new TestApplicationPaths(fixture.Path), new StubBackendClient());

        PaymentMatrixRow row = service.CreateDefaultMatrixRow(paymentMethod);

        Assert.Equal(expectedCountry.ToLowerInvariant() + "_" + paymentMethod, row.Name);
        Assert.Equal(paymentMethod == "gopay" ? "" : expectedCountry, row.RegistrationCountry);
        Assert.Equal(expectedCountry, row.CheckoutCountry);
        Assert.Equal(expectedPromotionCountry, row.PromotionCountry);
        Assert.Equal(expectedCountry, row.ProviderCountry);
        Assert.Equal(expectedApproveCountry, row.ApproveCountry);
        Assert.Equal(expectedCountry, row.RedirectCountry);
        Assert.Equal(1, row.SampleSize);
    }

    [Fact]
    public void LoadMatrixPreservesConfiguredApproveCountryWithoutCoercion()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), """
            {
              "protocol_payments": {
                "matrix": {
                  "cells": [
                    { "name": "gopay_custom", "payment_method": "gopay", "checkout_country": "ID", "approve_country": "US", "sample_size": 2 },
                    { "name": "momo_cell", "payment_method": "momo", "checkout_country": "JP", "approve_country": "VN" }
                  ]
                }
              }
            }
            """, new UTF8Encoding(false));
        var service = new PaymentBatchService(new TestApplicationPaths(fixture.Path), new StubBackendClient());

        IReadOnlyList<PaymentMatrixRow> rows = service.LoadMatrix("gopay");

        // The GoPay approve-country rule is owned by the Python backend
        // (payment_link_manager.coerce_approve_country); the desktop must pass
        // the configured value through unchanged.
        PaymentMatrixRow row = Assert.Single(rows);
        Assert.Equal("gopay_custom", row.Name);
        Assert.Equal("US", row.ApproveCountry);
        Assert.Equal(2, row.SampleSize);
    }

    [Fact]
    public void ProxyPoolsLoadAndSaveUnderMethodWithoutTouchingLegacyGlobalPool()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        File.WriteAllText(configPath, """
            {
              "protocol_payments": {
                "proxy_pool": ["http://legacy"],
                "methods": {
                  "gopay": {
                    "checkout_proxy_pool": ["http://checkout-old"],
                    "approve_proxy_pool": ["http://approve-old"],
                    "stage_proxy_countries": { "checkout": "ID", "approve": "TR" }
                  }
                }
              }
            }
            """, new UTF8Encoding(false));
        var service = new PaymentBatchService(new TestApplicationPaths(fixture.Path), new StubBackendClient());

        PaymentBatchProxyConfiguration loaded = service.LoadProxyConfiguration("gopay");
        Assert.Equal("http://checkout-old", loaded.CheckoutProxyPool);
        Assert.Equal("http://approve-old", loaded.ApproveProxyPool);
        Assert.Equal("ID", loaded.CheckoutCountry);
        Assert.Equal("TR", loaded.ApproveCountry);
        Assert.Equal("TH", loaded.UpdateCountry);

        SettingsSaveResult saved = service.SaveProxyConfiguration(
            "gopay",
            new PaymentBatchProxyConfiguration(
                "http://checkout-one\nhttp://checkout-two",
                "http://approve-jp\nhttp://approve-tr",
                "ID",
                "JP",
                "TR"));

        Assert.True(saved.Ok, saved.Error);
        JsonObject root = JsonNode.Parse(File.ReadAllText(configPath, Encoding.UTF8))!.AsObject();
        JsonObject method = root["protocol_payments"]!["methods"]!["gopay"]!.AsObject();
        Assert.Equal(
            new[] { "http://checkout-one", "http://checkout-two" },
            method["checkout_proxy_pool"]!.AsArray().Select(node => node!.GetValue<string>()).ToArray());
        Assert.Equal(
            new[] { "http://approve-jp", "http://approve-tr" },
            method["approve_proxy_pool"]!.AsArray().Select(node => node!.GetValue<string>()).ToArray());
        Assert.Equal("ID", method["stage_proxy_countries"]!["checkout"]!.GetValue<string>());
        Assert.Equal("JP", method["stage_proxy_countries"]!["approve"]!.GetValue<string>());
        Assert.Equal("TR", method["stage_proxy_countries"]!["promotion"]!.GetValue<string>());
        Assert.Equal("gopay_approve", method["stage_routes"]!["promotion"]!["pool"]!.GetValue<string>());
        Assert.Equal("TR", method["stage_routes"]!["promotion"]!["country"]!.GetValue<string>());
        Assert.Equal("http://legacy", root["protocol_payments"]!["proxy_pool"]![0]!.GetValue<string>());

        PaymentBatchProxyConfiguration reloaded = service.LoadProxyConfiguration("gopay");
        Assert.Equal("TR", reloaded.UpdateCountry);
    }

    [Fact]
    public void LegacyGlobalPoolIsParsedAsEntriesWhenMethodPoolIsAbsent()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(
            Path.Combine(fixture.Path, "config.json"),
            "{\"protocol_payments\":{\"proxy_pool\":[\"http://legacy-one\",\"http://legacy-two\"]}}",
            new UTF8Encoding(false));
        var service = new PaymentBatchService(new TestApplicationPaths(fixture.Path), new StubBackendClient());

        PaymentBatchProxyConfiguration loaded = service.LoadProxyConfiguration("gopay");

        Assert.Equal("http://legacy-one" + Environment.NewLine + "http://legacy-two", loaded.CheckoutProxyPool);
        Assert.Equal(loaded.CheckoutProxyPool, loaded.ApproveProxyPool);
    }

    [Fact]
    public void PaymentProxyPoolsNormalizeBareProviderEntriesAndPreserveSchemes()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        File.WriteAllText(configPath, "{}", new UTF8Encoding(false));
        var service = new PaymentBatchService(new TestApplicationPaths(fixture.Path), new StubBackendClient());

        SettingsSaveResult saved = service.SaveProxyConfiguration(
            "paypal",
            new PaymentBatchProxyConfiguration(
                "us.ipwo.net:7878:account_custom_zone_US:password",
                "socks5h://account_custom_zone_JP:password@as.ipwo.net:7878",
                "US",
                "JP",
                "GB"));

        Assert.True(saved.Ok, saved.Error);
        PaymentBatchProxyConfiguration loaded = service.LoadProxyConfiguration("paypal");
        Assert.Equal("http://account_custom_zone_US:password@us.ipwo.net:7878", loaded.CheckoutProxyPool);
        Assert.Equal("socks5h://account_custom_zone_JP:password@as.ipwo.net:7878", loaded.ApproveProxyPool);
    }

    [Fact]
    public void CanonicalNamedPoolsAndRoutesAreSharedBySingleAndBatchSurfaces()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        File.WriteAllText(configPath, """
            {
              "proxy": {
                "registration": "http://registration-jp",
                "pool": ["http://registration-jp"]
              },
              "protocol_payments": {
                "proxy_pools": {
                  "paypal_checkout": ["http://payment-us-one", "http://payment-us-two"],
                  "paypal_approve": ["http://payment-jp"]
                },
                "methods": {
                  "paypal": {
                    "stage_routes": {
                      "checkout": { "pool": "paypal_checkout", "country": "US" },
                      "approve": { "pool": "paypal_approve", "country": "JP" }
                    },
                    "stage_proxy_countries": { "checkout": "US", "approve": "JP" }
                  }
                }
              }
            }
            """, new UTF8Encoding(false));
        var service = new PaymentBatchService(new TestApplicationPaths(fixture.Path), new StubBackendClient());

        PaymentBatchProxyConfiguration loaded = service.LoadProxyConfiguration("paypal");

        Assert.Equal("http://payment-us-one" + Environment.NewLine + "http://payment-us-two", loaded.CheckoutProxyPool);
        Assert.Equal("http://payment-jp", loaded.ApproveProxyPool);
        Assert.Equal("US", loaded.CheckoutCountry);
        Assert.Equal("JP", loaded.ApproveCountry);
        JsonObject root = JsonNode.Parse(File.ReadAllText(configPath, Encoding.UTF8))!.AsObject();
        Assert.Equal("http://registration-jp", root["proxy"]!["registration"]!.GetValue<string>());
        Assert.DoesNotContain(
            root["proxy"]!["pool"]!.AsArray().Select(node => node!.GetValue<string>()),
            value => value.Contains("payment-", StringComparison.Ordinal));
    }

    private static string ArgumentAfter(IReadOnlyList<string> arguments, string option)
    {
        int index = arguments.ToList().IndexOf(option);
        Assert.True(index >= 0 && index + 1 < arguments.Count, $"Missing argument {option}");
        return arguments[index + 1];
    }

    private static JsonElement JsonElementOf(string json)
    {
        using JsonDocument document = JsonDocument.Parse(json);
        return document.RootElement.Clone();
    }
}

internal sealed class TemporaryDirectory : IDisposable
{
    public TemporaryDirectory()
    {
        Path = System.IO.Path.Combine(
            System.IO.Path.GetTempPath(),
            "smsworkbench-tests-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path);
    }

    public string Path { get; }

    public void Dispose()
    {
        if (Directory.Exists(Path))
            Directory.Delete(Path, true);
    }
}
