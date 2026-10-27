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
                "proxy_pool": ["http://legacy-payment"],
                "matrix": { "cells": [] },
                "methods": { "blik": { "blik_code": "123456" } }
              },
              "agent_identity": {
                "register_on_free_signup": true,
                "registration_timeout": 30,
                "import_note": "preserve"
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
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        Assert.Equal(42, root["unknown_extension"]!["keep"]!.GetValue<int>());
        Assert.Equal("http://primary", root["proxy"]!["registration"]!.GetValue<string>());
        Assert.Equal("http://primary", root["proxy"]!["default"]!.GetValue<string>());
        string[] proxyPool = root["proxy"]!["pool"]!.AsArray().Select(node => node!.GetValue<string>()).ToArray();
        Assert.Equal(ExpectedProxyOrder, proxyPool, StringComparer.OrdinalIgnoreCase);
        string[] paymentProxyPool = root["protocol_payments"]!["proxy_pool"]!.AsArray()
            .Select(node => node!.GetValue<string>())
            .ToArray();
        Assert.Equal(new[] { "http://legacy-payment" }, paymentProxyPool);
        Assert.Null(root["protocol_payments"]!["methods"]!["blik"]!["blik_code"]);
        Assert.Null(root["agent_identity"]!["register_on_free_signup"]);
        Assert.Null(root["agent_identity"]!["registration_timeout"]);
        Assert.Equal("preserve", root["agent_identity"]!["import_note"]!.GetValue<string>());
        Assert.Empty(Directory.GetFiles(fixture.Path, "config.json.tmp.*"));
        byte[] bytes = File.ReadAllBytes(configPath);
        Assert.False(bytes.Length >= 3 && bytes[0] == 0xEF && bytes[1] == 0xBB && bytes[2] == 0xBF);
    }

    [Fact]
    public void CatalogOmitsRegistrationAgentIdentitySettingsButKeepsExplicitImportMode()
    {
        Assert.DoesNotContain(
            SettingsCatalog.Categories.SelectMany(category => category.Sections),
            section => string.Equals(section.Title, "Agent Identity", StringComparison.Ordinal));
        Assert.DoesNotContain(
            SettingsCatalog.AllFields,
            field => field.Key.StartsWith("agent_identity_", StringComparison.Ordinal));

        SettingDefinition importMode = SettingsCatalog.AllFields.Single(field => field.Key == "sub2api_auth_mode");
        // `Options` is nullable on the record because non-enum settings have none;
        // this setting must expose the explicit-import option list.
        IReadOnlyList<string>? options = importMode.Options;
        Assert.NotNull(options);
        Assert.Contains("agent_identity", options);
    }

    [Fact]
    public void CatalogExposesSmailrApiKeyAsSecretSetting()
    {
        SettingDefinition apiKey = SettingsCatalog.AllFields.Single(field => field.Key == "smailr_api_key");
        Assert.Equal(SettingFieldKind.Secret, apiKey.Kind);
        Assert.Equal("email_registration.smailr.api_key", apiKey.JsonPath);

        SettingDefinition domain = SettingsCatalog.AllFields.Single(field => field.Key == "smailr_default_domain");
        Assert.Equal(SettingFieldKind.Options, domain.Kind);
        Assert.Equal(new[] { "smailr.com", "loc.cc", "mail.nodeloc.cc", "nodeloc.cc" }, domain.Options);
        Assert.DoesNotContain(SettingsCatalog.AllFields, field => field.Key == "remail_product_id");
    }

    [Fact]
    public void CatalogExposesIndependentBrowserRegistrationDriversAndSecrets()
    {
        SettingDefinition driver = SettingsCatalog.AllFields.Single(field => field.Key == "registration_driver");
        Assert.Equal(SettingFieldKind.Options, driver.Kind);
        Assert.Equal(
            new[] { "protocol", "playwright", "roxy", "cloak", "camoufox" },
            driver.Options);

        Assert.Equal(SettingFieldKind.Secret, SettingsCatalog.AllFields.Single(field => field.Key == "roxy_api_token").Kind);
        Assert.Equal(SettingFieldKind.Secret, SettingsCatalog.AllFields.Single(field => field.Key == "cloak_license_key").Kind);
        Assert.Contains(
            SettingsCatalog.Categories.Single(category => category.Title == "注册与接码").Sections,
            section => section.Title == "RoxyBrowser");
    }

    [Fact]
    public void BrowserDriverSettingsLoadAndSaveRoundTrip()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        File.WriteAllText(configPath, """
            {
              "registration": {
                "driver": "camoufox",
                "browser_headless": false,
                "browser_timeout_seconds": 75,
                "browser_locale": "en-GB",
                "browser_timezone": "Europe/London",
                "drivers": {
                  "roxy": { "api_token": "roxy-token", "workspace_id": "11" },
                  "cloak": { "license_key": "cloak-key", "humanize": false },
                  "camoufox": { "humanize": false, "max_width": 1366 }
                }
              }
            }
            """, new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));

        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();

        Assert.Equal("camoufox", Field(categories, "registration_driver").Value);
        Assert.False(Field(categories, "registration_browser_headless").BooleanValue);
        Assert.Equal("roxy-token", Field(categories, "roxy_api_token").Value);
        Assert.Equal("cloak-key", Field(categories, "cloak_license_key").Value);
        Assert.False(Field(categories, "camoufox_humanize").BooleanValue);

        Field(categories, "registration_driver").Value = "roxy";
        Field(categories, "registration_browser_headless").BooleanValue = true;
        Field(categories, "roxy_workspace_id").Value = "22";
        Field(categories, "camoufox_max_width").Value = "1440";
        SettingsSaveResult result = service.Save(categories);

        Assert.True(result.Ok, result.Error);
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        Assert.Equal("roxy", root["registration"]!["driver"]!.GetValue<string>());
        Assert.True(root["registration"]!["browser_headless"]!.GetValue<bool>());
        Assert.Equal("22", root["registration"]!["drivers"]!["roxy"]!["workspace_id"]!.GetValue<string>());
        Assert.Equal(1440, root["registration"]!["drivers"]!["camoufox"]!["max_width"]!.GetValue<int>());
    }

    [Fact]
    public void LoadUsesSmailrEnvironmentKeyWhenConfigValueIsEmpty()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), "{}", new UTF8Encoding(false));
        string? original = Environment.GetEnvironmentVariable("SMAILR_API_KEY");
        try
        {
            Environment.SetEnvironmentVariable("SMAILR_API_KEY", "test-env-key");
            var service = new SettingsService(new TestApplicationPaths(fixture.Path));

            IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();

            Assert.Equal("test-env-key", Field(categories, "smailr_api_key").Value);
        }
        finally
        {
            Environment.SetEnvironmentVariable("SMAILR_API_KEY", original);
        }
    }

    [Fact]
    public void LoadFormatsProxyPoolsAsOneEntryPerLine()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        File.WriteAllText(configPath, """
            {
              "proxy": { "pool": ["http://registration-one", "http://registration-two"] },
              "protocol_payments": {
                "proxy_pool": ["http://payment-one", "http://payment-two"],
                "matrix": { "cells": [] }
              }
            }
            """, new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));

        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();

        Assert.Equal(
            string.Join("\n", "http://registration-one", "http://registration-two"),
            Field(categories, "registration_proxy_pool").Value);
        Assert.DoesNotContain(SettingsCatalog.AllFields, field => field.Key == "protocol_proxy_pool");
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

    [Fact]
    public void SaveNormalizesBareProviderRegistrationPool()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        File.WriteAllText(configPath, "{}", new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));
        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();
        Field(categories, "registration_proxy").Value = "us.ipwo.net:7878:account_custom_zone_US:password";
        Field(categories, "registration_proxy_pool").Value =
            "as.ipwo.net:7878:account_custom_zone_JP:password\n" +
            "socks5h://account_custom_zone_GB:password@eu.ipwo.net:7878";
        Field(categories, "protocol_payment_matrix").Value = "{\"cells\":[]}";

        SettingsSaveResult result = service.Save(categories);

        Assert.True(result.Ok, result.Error);
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        Assert.Equal(
            "http://account_custom_zone_US:password@us.ipwo.net:7878",
            root["proxy"]!["registration"]!.GetValue<string>());
        Assert.Equal(
            new[]
            {
                "http://account_custom_zone_US:password@us.ipwo.net:7878",
                "http://account_custom_zone_JP:password@as.ipwo.net:7878",
                "socks5h://account_custom_zone_GB:password@eu.ipwo.net:7878"
            },
            root["proxy"]!["pool"]!.AsArray().Select(node => node!.GetValue<string>()).ToArray());
    }

    [Fact]
    public void CatalogOmitsLegacyProtocolProxyEditors()
    {
        Assert.DoesNotContain(SettingsCatalog.AllFields, field => field.Key == "protocol_proxy_pool");
        Assert.DoesNotContain(SettingsCatalog.AllFields, field => field.Key.StartsWith("protocol_", StringComparison.Ordinal)
            && field.Key.EndsWith("_proxy", StringComparison.Ordinal));
    }

    [Fact]
    public void CatalogExposesPulseCanaryAndCrossBatchRetryPolicy()
    {
        SettingDefinition canary = SettingsCatalog.AllFields.Single(
            field => field.Key == "pulse_canary_enabled");
        Assert.Equal(SettingFieldKind.Boolean, canary.Kind);
        Assert.Equal("registration.pulse.canary_enabled", canary.JsonPath);

        SettingDefinition cooldown = SettingsCatalog.AllFields.Single(
            field => field.Key == "registration_cross_batch_cooldown");
        Assert.Equal(SettingFieldKind.Number, cooldown.Kind);
        Assert.Equal(
            "registration.retry_policy.cross_batch_cooldown_seconds",
            cooldown.JsonPath);

        SettingDefinition quarantine = SettingsCatalog.AllFields.Single(
            field => field.Key == "registration_otp_pending_quarantine");
        Assert.Equal(SettingFieldKind.Number, quarantine.Kind);
        Assert.Equal(
            "registration.retry_policy.otp_pending_quarantine_threshold",
            quarantine.JsonPath);
    }

    [Fact]
    public void SaveDoesNotOverwriteSmsBowerBusinessDefaults()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        File.WriteAllText(configPath, """
            {
              "phone_reuse": {
                "smsbower": { "service": "custom", "service_name": "Custom Service" }
              }
            }
            """, new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));
        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();

        SettingsSaveResult result = service.Save(categories);

        Assert.True(result.Ok, result.Error);
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        // Python already defaults these (smsbower.OPENAI_SERVICE_CODE / display-only
        // metadata), so Save must leave operator values untouched.
        Assert.Equal("custom", root["phone_reuse"]!["smsbower"]!["service"]!.GetValue<string>());
        Assert.Equal("Custom Service", root["phone_reuse"]!["smsbower"]!["service_name"]!.GetValue<string>());
        // Pinned values whose Python-side default differs stay enforced.
        Assert.Equal("smsbower", root["phone_reuse"]!["source"]!.GetValue<string>());
        Assert.Equal("purchase", root["email_registration"]!["remail"]!["service_mode"]!.GetValue<string>());
    }

    [Fact]
    public void SaveLeavesMissingSmsBowerServiceKeysAbsent()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        File.WriteAllText(configPath, "{}", new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));
        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();

        SettingsSaveResult result = service.Save(categories);

        Assert.True(result.Ok, result.Error);
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        // The catalog fields create phone_reuse.smsbower, but the previously forced
        // business values must stay absent so Python-side defaults keep applying.
        Assert.Null(root["phone_reuse"]!["smsbower"]!["service"]);
        Assert.Null(root["phone_reuse"]!["smsbower"]!["service_name"]);
    }

    [Fact]
    public void GetStringReadsDottedPathsCaseInsensitivelyAndAppliesFallback()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        File.WriteAllText(configPath, """
            {
              "proxy": { "registration": "http://primary", "pool": ["http://a", "http://b"] },
              "storage": { "sqlite_path": "runtime/accounts.sqlite3" },
              "protocol_payments": { "methods": { "momo": { "timeout_seconds": 120 } } }
            }
            """, new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));

        Assert.Equal("http://primary", service.GetString("proxy.registration"));
        Assert.Equal("http://primary", service.GetString("Proxy.Registration"));
        Assert.Equal("runtime/accounts.sqlite3", service.GetString("storage.sqlite_path"));
        Assert.Equal("120", service.GetString("protocol_payments.methods.momo.timeout_seconds"));
        Assert.Equal("", service.GetString("proxy.missing"));
        Assert.Equal("fallback", service.GetString("proxy.missing", "fallback"));
        Assert.Equal(new[] { "http://a", "http://b" }, service.GetStringList("proxy.pool"));
        Assert.Empty(service.GetStringList("proxy.missing"));
    }

    [Fact]
    public void GetStringNeverCreatesConfigAndToleratesMalformedJson()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));

        Assert.Equal("fallback", service.GetString("proxy.registration", "fallback"));
        Assert.Empty(service.GetStringList("proxy.pool"));
        Assert.False(File.Exists(configPath));

        File.WriteAllText(configPath, "{not-json", new UTF8Encoding(false));
        Assert.Equal("fallback", service.GetString("proxy.registration", "fallback"));
        Assert.Empty(service.GetStringList("proxy.pool"));
    }

    [Fact]
    public void PathSettingsLoadAndSaveAsRepositoryRelativePaths()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        string pythonPath = Path.Combine(fixture.Path, ".venv", "Scripts", "python.exe");
        string mailboxPath = Path.Combine(fixture.Path, "data", "mailbox_tokens.txt");
        File.WriteAllText(configPath, $$"""
            {
              "runtime": { "python_path": "{{pythonPath.Replace("\\", "\\\\")}}" },
              "email_registration": { "token_file": "{{mailboxPath.Replace("\\", "\\\\")}}" }
            }
            """, new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));

        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();

        Assert.Equal(".venv/Scripts/python.exe", Field(categories, "python_path").Value);
        Assert.Equal("data/mailbox_tokens.txt", Field(categories, "token_file").Value);

        SettingsSaveResult result = service.Save(categories);

        Assert.True(result.Ok, result.Error);
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        Assert.Equal(".venv/Scripts/python.exe", root["runtime"]!["python_path"]!.GetValue<string>());
        Assert.Equal("data/mailbox_tokens.txt", root["email_registration"]!["token_file"]!.GetValue<string>());
    }

    [Fact]
    public void ExternalPythonPathRemainsAbsolute()
    {
        using var fixture = new TemporaryDirectory();
        string externalPath = Path.Combine(Path.GetTempPath(), "python.exe");
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), $$"""
            { "runtime": { "python_path": "{{externalPath.Replace("\\", "\\\\")}}" } }
            """, new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));

        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();

        Assert.Equal(externalPath, Field(categories, "python_path").Value);
        SettingsSaveResult result = service.Save(categories);
        Assert.True(result.Ok, result.Error);
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        Assert.Equal(externalPath, root["runtime"]!["python_path"]!.GetValue<string>());
    }

    [Fact]
    public void UpdateConfigPreservesUnknownFieldsAndWritesAtomically()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        File.WriteAllText(configPath, """
            {
              "unknown_extension": { "keep": 42 },
              "phone_reuse": { "smsbower": { "country_prefix": "+233", "country": "38" } }
            }
            """, new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));

        service.UpdateConfig(root =>
        {
            JsonObject smsBower = root["phone_reuse"]!["smsbower"]!.AsObject();
            smsBower["country"] = "22";
            smsBower.Remove("country_prefix");
        });

        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        Assert.Equal(42, root["unknown_extension"]!["keep"]!.GetValue<int>());
        Assert.Equal("22", root["phone_reuse"]!["smsbower"]!["country"]!.GetValue<string>());
        Assert.Null(root["phone_reuse"]!["smsbower"]!["country_prefix"]);
        Assert.Empty(Directory.GetFiles(fixture.Path, "config.json.tmp.*"));
        byte[] bytes = File.ReadAllBytes(configPath);
        Assert.False(bytes.Length >= 3 && bytes[0] == 0xEF && bytes[1] == 0xBB && bytes[2] == 0xBF);
    }

    [Fact]
    public void UpdateConfigCreatesMissingConfigFile()
    {
        using var fixture = new TemporaryDirectory();
        string configPath = Path.Combine(fixture.Path, "config.json");
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));

        service.UpdateConfig(root => root["phone_reuse"] = new JsonObject { ["source"] = "smsbower" });

        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        Assert.Equal("smsbower", root["phone_reuse"]!["source"]!.GetValue<string>());
    }

    /// <summary>
    /// 🔴 Regression test for the 2026-09-23 cross-provider key overwrite.
    ///
    /// <para>
    /// The 供应商 dropdown and the API Key box are separate controls. Loading
    /// resolves provider-scoped paths once, so without a reload the box keeps
    /// showing the provider selected at open time; picking another provider and
    /// saving then writes that value into the newly selected section. Measured
    /// on 2026-09-23: one pass through the dropdown put smsbower's key into
    /// `phone_reuse.herosms`, `.grizzly` and `.nexsms`, and all three vendors
    /// answered with a credential error (401 BAD_KEY / NO_KEY / 401).
    /// </para>
    ///
    /// <para>
    /// Driven through <see cref="SettingsViewModel"/> rather than by calling
    /// <c>ReloadProviderScopedFields</c> directly, because the subscription that
    /// makes it happen is the part that was missing -- a direct call would pass
    /// against the unfixed view model.
    /// </para>
    /// </summary>
    [Fact]
    public void SwitchingProviderReloadsProviderScopedBoxesSoSaveCannotStampAnotherProvidersKey()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), """
            {
              "phone_reuse": {
                "source": "smsbower",
                "smsbower": { "api_key": "bower-key", "sms_timeout": 60 },
                "herosms": { "api_key": "hero-key", "sms_timeout": 120, "endpoint": "https://hero" }
              }
            }
            """, new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));
        var viewModel = new SettingsViewModel(service, new StubFileLauncher());

        Assert.Equal("bower-key", Field(viewModel.Categories, "phone_provider_api_key").Value);
        Assert.Equal("60", Field(viewModel.Categories, "phone_provider_sms_timeout").Value);

        Field(viewModel.Categories, "phone_provider").Value = "herosms";

        // Reloaded, not carried over.
        Assert.Equal("hero-key", Field(viewModel.Categories, "phone_provider_api_key").Value);
        Assert.Equal("120", Field(viewModel.Categories, "phone_provider_sms_timeout").Value);
        Assert.Equal("https://hero", Field(viewModel.Categories, "phone_provider_endpoint").Value);

        SettingsSaveResult result = service.Save(viewModel.Categories);

        Assert.True(result.Ok, result.Error);
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        Assert.Equal("herosms", root["phone_reuse"]!["source"]!.GetValue<string>());
        Assert.Equal("hero-key", root["phone_reuse"]!["herosms"]!["api_key"]!.GetValue<string>());
        // The negative half: before the fix this leaf held "bower-key", i.e. the
        // wrong provider's credential sitting under the right section name.
        Assert.Equal("bower-key", root["phone_reuse"]!["smsbower"]!["api_key"]!.GetValue<string>());
    }

    /// <summary>
    /// The reload subscription is keyed on a field key string, so a catalog
    /// rename would silently stop it firing -- the failure mode being "the box
    /// keeps the previous provider's key" again. Pin the coupling.
    /// </summary>
    [Fact]
    public void ProviderSelectorFieldIsAnOptionsFieldOnPhoneReuseSource()
    {
        using var fixture = new TemporaryDirectory();
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));
        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();

        SettingFieldViewModel selector = Field(categories, SettingsViewModel.ProviderSelectorKey);

        Assert.Equal(SettingFieldKind.Options, selector.Kind);
        Assert.Equal("phone_reuse.source", selector.Definition.JsonPath);
        // And the token the reload filters on must actually be present, or every
        // provider-scoped box is skipped.
        Assert.Contains(
            categories.SelectMany(category => category.Sections).SelectMany(section => section.Fields),
            field => field.Definition.JsonPath == "phone_reuse.{provider}.api_key");
    }

    [Fact]
    public void SaveWritesTheEffectiveLaneAndMirrorsTheLegacyKeys()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), """
            {
              "proxy": {
                "registration": "http://legacy",
                "lanes": {
                  "protocol_registration": { "pool": [ "http://legacy" ] }
                }
              }
            }
            """, new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));
        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();
        Field(categories, "registration_proxy").Value = "http://new";
        Field(categories, "registration_proxy_pool").Value = "http://new2" + Environment.NewLine + "http://new";

        SettingsSaveResult result = service.Save(categories);

        Assert.True(result.Ok, result.Error);
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        string[] lane = root["proxy"]!["lanes"]!["protocol_registration"]!["pool"]!
            .AsArray().Select(node => node!.GetValue<string>()).ToArray();
        Assert.Equal(new[] { "http://new", "http://new2" }, lane, StringComparer.OrdinalIgnoreCase);
        Assert.Equal("http://new", root["proxy"]!["registration"]!.GetValue<string>());
        string[] legacyPool = root["proxy"]!["pool"]!.AsArray()
            .Select(node => node!.GetValue<string>()).ToArray();
        Assert.Equal(new[] { "http://new", "http://new2" }, legacyPool, StringComparer.OrdinalIgnoreCase);
    }

    [Fact]
    public void SaveWithoutALaneCreatesTheLaneFromTheBox()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), """
            { "proxy": { "pool": [ "http://old" ] } }
            """, new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));
        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();
        Field(categories, "registration_proxy").Value = "http://primary";

        SettingsSaveResult result = service.Save(categories);

        Assert.True(result.Ok, result.Error);
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        Assert.Equal("http://primary", root["proxy"]!["registration"]!.GetValue<string>());
        string[] lane = root["proxy"]!["lanes"]!["protocol_registration"]!["pool"]!
            .AsArray().Select(node => node!.GetValue<string>()).ToArray();
        Assert.Contains("http://primary", lane, StringComparer.OrdinalIgnoreCase);
    }

    [Fact]
    public void SaveBrowserAndPaymentQualificationPoolsWritesTheirLanes()
    {
        using var fixture = new TemporaryDirectory();
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));
        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();
        Field(categories, "browser_registration_proxy").Value = "http://browser:4";
        Field(categories, "browser_registration_proxy_pool").Value = "http://browser:5";
        Field(categories, "payment_qualification_proxy_pool").Value = "http://stripe:6";

        SettingsSaveResult result = service.Save(categories);

        Assert.True(result.Ok, result.Error);
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        string[] browserLane = root["proxy"]!["lanes"]!["browser_registration"]!["pool"]!
            .AsArray().Select(node => node!.GetValue<string>()).ToArray();
        Assert.Equal(new[] { "http://browser:4", "http://browser:5" }, browserLane);
        string[] paymentLane = root["proxy"]!["lanes"]!["payment_qualification"]!["pool"]!
            .AsArray().Select(node => node!.GetValue<string>()).ToArray();
        Assert.Equal(new[] { "http://stripe:6" }, paymentLane);
        Assert.Equal(
            new[] { "http://stripe:6" },
            root["payment_qualification"]!["proxy_pool"]!.AsArray()
                .Select(node => node!.GetValue<string>()).ToArray());
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

    /// <summary>
    /// An empty proxy box must stay empty. It used to be normalised to a
    /// hardcoded <c>http://127.0.0.1:7897</c>, which the window then forwarded as
    /// an explicit <c>--proxy</c> -- suppressing the backend's lane pools and
    /// pointing fresh installs at a proxy that need not exist.
    /// </summary>
    [Fact]
    public void SaveWithBlankProxyFieldsDoesNotInventALoopbackProxy()
    {
        using var fixture = new TemporaryDirectory();
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));
        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();
        Field(categories, "registration_proxy").Value = "";
        Field(categories, "registration_proxy_pool").Value = "";
        Field(categories, "mailbox_proxy").Value = "";
        Field(categories, "mailbox_proxy_pool").Value = "";

        SettingsSaveResult result = service.Save(categories);

        Assert.True(result.Ok, result.Error);
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        Assert.Equal("", root["proxy"]?["registration"]?.GetValue<string>() ?? "");
        Assert.Equal("", root["proxy"]?["default"]?.GetValue<string>() ?? "");
        Assert.Empty(root["proxy"]!["pool"]!.AsArray());
        // Scope the loopback check to the proxy section: the merged config legitimately
        // carries other local addresses (e.g. the mail OTP web service api_base).
        Assert.DoesNotContain("127.0.0.1", root["proxy"]!.ToJsonString());
    }

    [Fact]
    public void LoadWithoutAConfiguredProxyLeavesTheBoxEmpty()
    {
        using var fixture = new TemporaryDirectory();
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));

        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();

        Assert.Equal("", Field(categories, "registration_proxy").Value);
        Assert.Equal("", Field(categories, "mailbox_proxy").Value);
    }

    /// <summary>
    /// PayPal is a different egress lane. The registration box used to fall back
    /// to <c>paypal.proxies[0]</c> for display, and <c>Save</c> then persisted
    /// that value into <c>proxy.registration</c>, <c>proxy.default</c>,
    /// <c>proxy.pool</c> and <c>phone_reuse.proxy</c> -- so opening Settings and
    /// pressing Save silently moved the signup lane onto the checkout exit.
    /// </summary>
    [Fact]
    public void PayPalProxyIsNeverShownOrPromotedAsTheRegistrationProxy()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(
            Path.Combine(fixture.Path, "config.json"),
            """
            {
              "paypal": { "proxies": ["socks5h://paypal.example:1080"] }
            }
            """,
            new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));

        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();
        Assert.Equal("", Field(categories, "registration_proxy").Value);

        SettingsSaveResult result = service.Save(categories);

        Assert.True(result.Ok, result.Error);
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        Assert.Equal("", root["proxy"]?["registration"]?.GetValue<string>() ?? "");
        Assert.Equal("", root["proxy"]?["default"]?.GetValue<string>() ?? "");
        Assert.Equal("", root["phone_reuse"]?["proxy"]?.GetValue<string>() ?? "");
        // The PayPal lane keeps its own route untouched by the round trip.
        Assert.Equal(
            "socks5h://paypal.example:1080",
            root["paypal"]!["proxies"]![0]!.GetValue<string>());
    }

    /// <summary>
    /// <c>proxy.lanes.&lt;lane&gt;</c> is authoritative in the backend whenever it is
    /// non-empty (<c>proxy/routing.py::canonical_lane_pool</c>, and the same rule
    /// for <c>mailbox/router.py</c>). This page cannot edit it, so saving with an
    /// empty box must neither invent a legacy route nor clear the phone exit that
    /// has no field of its own.
    /// </summary>
    [Fact]
    public void SaveWithBlankProxyBoxesPreservesDeclaredLanesAndThePhoneExit()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(
            Path.Combine(fixture.Path, "config.json"),
            """
            {
              "proxy": {
                "lanes": {
                  "protocol_registration": ["http://lane-reg:1"],
                  "mailbox": ["http://lane-mb:2"]
                }
              },
              "phone_reuse": { "proxy": "http://phone-exit:3" }
            }
            """,
            new UTF8Encoding(false));
        var service = new SettingsService(new TestApplicationPaths(fixture.Path));

        IReadOnlyList<SettingsCategoryViewModel> categories = service.Load();
        Assert.Equal(
            new[] { "protocol_registration", "mailbox" },
            service.AuthoritativeProxyLanes);
        // The boxes now read the lane they own, so opening Settings shows the
        // effective route instead of a misleading blank.
        Assert.Equal("http://lane-reg:1", Field(categories, "registration_proxy").Value);
        Assert.Equal("http://lane-mb:2", Field(categories, "mailbox_proxy").Value);

        SettingsSaveResult result = service.Save(categories);

        Assert.True(result.Ok, result.Error);
        JsonObject root = ConfigTestHelpers.ReadMergedConfig(fixture.Path);
        Assert.Equal(
            "http://lane-reg:1",
            root["proxy"]!["lanes"]!["protocol_registration"]!["pool"]![0]!.GetValue<string>());
        Assert.Equal(
            "http://lane-mb:2",
            root["proxy"]!["lanes"]!["mailbox"]!["pool"]![0]!.GetValue<string>());
        Assert.Equal("http://phone-exit:3", root["phone_reuse"]!["proxy"]!.GetValue<string>());
        // The box now reads the lane, so a save mirrors that same value into the
        // legacy key; the explicit, separate phone exit above is not overwritten.
        Assert.Equal("http://lane-reg:1", root["proxy"]!["registration"]!.GetValue<string>());
    }

    [Fact]
    public void DetectAuthoritativeProxyLanes_MatchesTheBackendShapeRules()
    {
        JsonObject root = JsonNode.Parse("""
            {
              "proxy": { "lanes": {
                "protocol_registration": ["http://a:1"],
                "browser_registration": { "pool": ["http://b:2"] },
                "mailbox": { "default": "http://c:3" },
                "payment": ["http://ignored:4"]
              } }
            }
            """)!.AsObject();

        IReadOnlyList<string> lanes = SettingsService.DetectAuthoritativeProxyLanes(root);

        Assert.Equal(
            new[] { "protocol_registration", "browser_registration", "mailbox" },
            lanes);
    }

    [Fact]
    public void DetectAuthoritativeProxyLanes_IgnoresBlankAndAbsentEntries()
    {
        JsonObject blank = JsonNode.Parse("""
            {
              "proxy": { "lanes": {
                "protocol_registration": [],
                "browser_registration": { "pool": [] },
                "mailbox": "   "
              } }
            }
            """)!.AsObject();
        JsonObject absent = JsonNode.Parse("""{ "proxy": { "registration": "http://legacy:1" } }""")!.AsObject();

        Assert.Empty(SettingsService.DetectAuthoritativeProxyLanes(blank));
        Assert.Empty(SettingsService.DetectAuthoritativeProxyLanes(absent));
    }
}
