using System.Text.Json.Nodes;

namespace SmsWorkbench.Tests;

/// <summary>
/// The desktop's one direct-HTTP entry point resolves its proxy from a config
/// value an operator edits by hand, so the resolution is pinned separately from
/// the socket.
/// </summary>
/// <remarks>
/// Regression: on 2026-11-02 <see cref="DesktopEgress"/> read
/// <c>proxy.lanes.mailbox</c> — a <b>JsonObject</b> in the canonical shape —
/// instead of its <c>.pool</c>. <c>GetStringList</c> stringified the object to
/// JSON text, <c>new WebProxy(...)</c> threw <c>UriFormatException</c> in the
/// constructor, and the window never opened at all. A config value must be able
/// to cost the proxy, never the process.
/// </remarks>
public sealed class DesktopEgressTests
{
    [Fact]
    public void A_declared_lane_opens_the_window_and_normalises_host_port()
    {
        using var directory = new TempDirectory();
        WriteMerged(directory.Path, new JsonObject
        {
            ["proxy"] = new JsonObject
            {
                ["lanes"] = new JsonObject
                {
                    // `host:port` is what an operator types; WebProxy needs a scheme.
                    ["mailbox"] = new JsonObject { ["pool"] = new JsonArray("127.0.0.1:7897") },
                },
            },
        });

        // Construction is the regression: it used to throw before any request.
        using var egress = new DesktopEgress(Settings(directory.Path), Logger());

        Assert.Equal(
            "http://127.0.0.1:7897",
            DesktopEgress.ResolveMailboxLaneProxy(Settings(directory.Path), Logger()));
    }

    [Fact]
    public void A_legacy_lane_string_is_still_honoured()
    {
        using var directory = new TempDirectory();
        WriteMerged(directory.Path, new JsonObject
        {
            ["proxy"] = new JsonObject
            {
                ["lanes"] = new JsonObject { ["mailbox"] = "socks5://10.0.0.2:1080" },
            },
        });

        Assert.Equal(
            "socks5://10.0.0.2:1080",
            DesktopEgress.ResolveMailboxLaneProxy(Settings(directory.Path), Logger()));
    }

    [Fact]
    public void The_pre_lane_top_level_keys_are_still_honoured()
    {
        using var directory = new TempDirectory();
        WriteMerged(directory.Path, new JsonObject { ["mailbox_proxy"] = "http://10.0.0.3:8080" });

        Assert.Equal(
            "http://10.0.0.3:8080",
            DesktopEgress.ResolveMailboxLaneProxy(Settings(directory.Path), Logger()));
    }

    [Fact]
    public void A_malformed_entry_degrades_to_direct_instead_of_throwing()
    {
        using var directory = new TempDirectory();
        WriteMerged(directory.Path, new JsonObject
        {
            ["proxy"] = new JsonObject
            {
                ["lanes"] = new JsonObject
                {
                    ["mailbox"] = new JsonObject { ["pool"] = new JsonArray("not a proxy") },
                },
            },
        });

        Assert.Equal("", DesktopEgress.ResolveMailboxLaneProxy(Settings(directory.Path), Logger()));
        // ...and the unusable value does not stop construction either.
        using var egress = new DesktopEgress(Settings(directory.Path), Logger());
    }

    [Fact]
    public void An_empty_configuration_means_direct()
    {
        using var directory = new TempDirectory();

        Assert.Equal("", DesktopEgress.ResolveMailboxLaneProxy(Settings(directory.Path), Logger()));
    }

    private static SettingsService Settings(string root) => new(new TestApplicationPaths(root));

    private static Serilog.Core.Logger Logger() => new Serilog.LoggerConfiguration().CreateLogger();

    /// <summary>Write a merged config object through the shard splitter the app uses.</summary>
    private static void WriteMerged(string root, JsonObject merged)
        => ConfigStore.WriteShards(new TestApplicationPaths(root), merged);

    private sealed class TempDirectory : IDisposable
    {
        public TempDirectory()
        {
            Path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "grt-egress-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(Path);
        }

        public string Path { get; }

        public void Dispose()
        {
            try
            {
                Directory.Delete(Path, recursive: true);
            }
            catch (IOException)
            {
                // A locked temp directory is not this test's failure to report.
            }
        }
    }
}
