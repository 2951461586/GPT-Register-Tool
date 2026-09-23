using System.Text;
using System.Text.Json.Nodes;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class ConfigStoreTests
{
    [Fact]
    public void WriteShardsWritesAnEmptyShardFileRatherThanDeletingIt()
    {
        using var fixture = new TemporaryDirectory();
        var paths = new TestApplicationPaths(fixture.Path);
        var root = new JsonObject
        {
            // proxy.json owns "proxy"; everything else here lands in runtime.json.
            ["proxy"] = new JsonObject { ["default"] = "http://primary" },
            ["runtime"] = new JsonObject { ["python_path"] = ".venv/Scripts/python.exe" }
        };

        ConfigStore.WriteShards(paths, root);

        Assert.True(File.Exists(Path.Combine(fixture.Path, ConfigStore.ProxyShard)));
        Assert.True(File.Exists(Path.Combine(fixture.Path, ConfigStore.RuntimeShard)));

        // The empty shard is written as {} rather than deleted. "At least one shard
        // file exists" is how both languages decide the sharded layout is in use, so
        // a missing file is not a neutral outcome -- it flips the app to the legacy
        // single-file branch (see ClearingEverySettingDoesNotHandTheAppBackToTheLegacyFile).
        string payment = Path.Combine(fixture.Path, ConfigStore.PaymentShard);
        Assert.True(File.Exists(payment));
        Assert.Equal("{}", File.ReadAllText(payment, Encoding.UTF8).Trim());
    }

    [Fact]
    public void DeletingLastKeyOfAShardDoesNotResurrectItOnTheNextRead()
    {
        using var fixture = new TemporaryDirectory();
        var paths = new TestApplicationPaths(fixture.Path);

        var seeded = new JsonObject
        {
            ["proxy"] = new JsonObject { ["default"] = "http://primary" },
            ["protocol_payments"] = new JsonObject
            {
                ["enabled_methods"] = new JsonArray("blik", "momo")
            }
        };
        ConfigStore.WriteShards(paths, seeded);
        Assert.True(File.Exists(Path.Combine(fixture.Path, ConfigStore.PaymentShard)));

        // The caller removes the only payment key, leaving the payment shard empty.
        JsonObject? merged = ConfigStore.ReadMerged(paths);
        Assert.NotNull(merged);
        merged.Remove("protocol_payments");
        ConfigStore.WriteShards(paths, merged);

        string payment = Path.Combine(fixture.Path, ConfigStore.PaymentShard);
        Assert.True(File.Exists(payment));
        Assert.Equal("{}", File.ReadAllText(payment, Encoding.UTF8).Trim());

        // Reading again must not bring the deleted key back from a stale file.
        JsonObject? reread = ConfigStore.ReadMerged(paths);
        Assert.NotNull(reread);
        Assert.False(reread.ContainsKey("protocol_payments"));
        Assert.True(reread.ContainsKey("proxy"));
    }

    [Fact]
    public void ClearingEverySettingDoesNotHandTheAppBackToTheLegacyFile()
    {
        using var fixture = new TemporaryDirectory();
        var paths = new TestApplicationPaths(fixture.Path);

        // A legacy single-file config that must never be consulted again once the
        // sharded layout exists -- not even with every setting cleared. Deleting the
        // empty shards (the previous behavior) made AnyShardExists() false, which
        // sent ReadMerged down the legacy branch and resurrected exactly this file.
        File.WriteAllText(
            Path.Combine(fixture.Path, ConfigStore.LegacyConfig),
            """{"proxy": {"default": "http://legacy-must-not-win"}}""",
            new UTF8Encoding(false));

        ConfigStore.WriteShards(paths, new JsonObject());

        JsonObject? merged = ConfigStore.ReadMerged(paths);
        Assert.NotNull(merged);
        Assert.False(merged.ContainsKey("proxy"));
    }

    [Fact]
    public void WriteShardsLeavesUnchangedShardsAlone()
    {
        using var fixture = new TemporaryDirectory();
        var paths = new TestApplicationPaths(fixture.Path);
        var root = new JsonObject
        {
            ["proxy"] = new JsonObject { ["default"] = "http://primary" },
            ["runtime"] = new JsonObject { ["python_path"] = ".venv/Scripts/python.exe" }
        };
        ConfigStore.WriteShards(paths, root);

        string runtimePath = Path.Combine(fixture.Path, ConfigStore.RuntimeShard);
        string proxyPath = Path.Combine(fixture.Path, ConfigStore.ProxyShard);
        DateTime runtimeStamp = File.GetLastWriteTimeUtc(runtimePath);
        string runtimeBefore = File.ReadAllText(runtimePath, Encoding.UTF8);

        // Only proxy.json's content changes.
        root["proxy"] = new JsonObject { ["default"] = "http://secondary" };
        IReadOnlyList<string> written = ConfigStore.WriteShards(paths, root);

        Assert.Single(written);
        Assert.Equal(ConfigStore.ProxyShard, written[0]);
        // The untouched shard keeps its timestamp -- that mtime is the audit signal
        // for "which part of the config was edited" -- and is not handed a fresh
        // .bak, which would otherwise discard the last real backup.
        Assert.Equal(runtimeStamp, File.GetLastWriteTimeUtc(runtimePath));
        Assert.Equal(runtimeBefore, File.ReadAllText(runtimePath, Encoding.UTF8));
        Assert.False(File.Exists(runtimePath + ".bak"));
        // The changed shard does get one, matching Python's shutil.copy2.
        Assert.True(File.Exists(proxyPath + ".bak"));
        Assert.True(
            File.ReadAllText(proxyPath + ".bak", Encoding.UTF8).Contains("http://primary", StringComparison.Ordinal),
            "the backup must hold the content that was replaced");
    }

    [Fact]
    public void WriteShardsEmitsLiteralNonAsciiAndPlusLikePython()
    {
        // Written as escapes so this test file stays pure ASCII no matter how the
        // compiler guesses its encoding.
        const string chile = "\u667A\u5229";       // 智利
        // Synthetic number from the NANP reserved fictional range (555-0100..
        // 555-0199), not a real relay number. An earlier revision of this test
        // copied a live PayPal relay phone straight out of payment.json, which
        // put a real number into a public repository; the only property this
        // test needs is a leading '+' that must survive as a literal.
        const string phone = "\u002B15555550100";  // +15555550100

        using var fixture = new TemporaryDirectory();
        var paths = new TestApplicationPaths(fixture.Path);
        var root = new JsonObject
        {
            ["phone_reuse"] = new JsonObject
            {
                ["smsbower"] = new JsonObject { ["country_name_zh"] = chile, ["phone"] = phone }
            }
        };

        ConfigStore.WriteShards(paths, root);

        string text = File.ReadAllText(Path.Combine(fixture.Path, ConfigStore.ProxyShard), Encoding.UTF8);
        // Python writes with json.dumps(..., ensure_ascii=False), i.e. literal
        // characters. The default JavaScriptEncoder escaped both of these, so every
        // desktop save rewrote the file's non-ASCII bytes and the two writers' change
        // detection would have reported each other's files as modified forever.
        Assert.True(text.Contains(chile, StringComparison.Ordinal), "CJK must be written literally");
        Assert.True(text.Contains(phone, StringComparison.Ordinal), "'+' must be written literally");
        Assert.False(text.Contains("\\u667A", StringComparison.Ordinal), "CJK must not be \\u-escaped");
        Assert.False(text.Contains("\\u002B", StringComparison.Ordinal), "'+' must not be \\u-escaped");
        // Part of the same contract: Python writes `json.dumps(...) + "\n"`, and both
        // writers compare payloads byte for byte to decide whether a shard changed.
        Assert.True(text.EndsWith("\n", StringComparison.Ordinal), "shards end with a newline");
    }

    [Fact]
    public void ReadMergedIsCaseInsensitiveForNestedLookups()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, ConfigStore.ProxyShard), """
            {
              "proxy": { "registration": "http://reg", "default": "http://primary" }
            }
            """, new UTF8Encoding(false));
        File.WriteAllText(Path.Combine(fixture.Path, ConfigStore.RuntimeShard), "{}", new UTF8Encoding(false));
        var paths = new TestApplicationPaths(fixture.Path);

        JsonObject? merged = ConfigStore.ReadMerged(paths);

        Assert.NotNull(merged);
        // JsonObject built with new() does not inherit PropertyNameCaseInsensitive;
        // ReadMerged must round-trip so this lookup keeps working.
        Assert.True(merged.TryGetPropertyValue("Proxy", out JsonNode? node));
        Assert.NotNull(node);
    }
}
