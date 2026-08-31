using System.Text;
using System.Text.Json.Nodes;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class ConfigStoreTests
{
    [Fact]
    public void WriteShardsRemovesShardFileWhenItEndsUpEmpty()
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
        Assert.False(File.Exists(Path.Combine(fixture.Path, ConfigStore.PaymentShard)));
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

        Assert.False(File.Exists(Path.Combine(fixture.Path, ConfigStore.PaymentShard)));

        // Reading again must not bring the deleted key back from a stale file.
        JsonObject? reread = ConfigStore.ReadMerged(paths);
        Assert.NotNull(reread);
        Assert.False(reread.ContainsKey("protocol_payments"));
        Assert.True(reread.ContainsKey("proxy"));
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
