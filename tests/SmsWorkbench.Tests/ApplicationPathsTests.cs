using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class ApplicationPathsTests
{
    [Fact]
    public void FindsRepositoryRootFromPublishedDirectory()
    {
        string root = Path.Combine(Path.GetTempPath(), "smsworkbench-paths-" + Guid.NewGuid().ToString("N"));
        string published = Path.Combine(root, "dist", "net10");
        Directory.CreateDirectory(published);
        File.WriteAllText(Path.Combine(root, "chatgpt_phone_reg.py"), "");
        try
        {
            var paths = new ApplicationPaths(published);

            Assert.Equal(root, paths.RootDirectory);
            Assert.Equal(Path.Combine(root, "chatgpt_phone_reg.py"), paths.BackendScriptPath);
        }
        finally
        {
            Directory.Delete(root, true);
        }
    }

    [Fact]
    public void ExplicitConfigDirectoryIsUsedAndNeverPullsFilesOutOfTheCheckout()
    {
        string root = Path.Combine(Path.GetTempPath(), "smsworkbench-paths-" + Guid.NewGuid().ToString("N"));
        string chosen = Path.Combine(root, "chosen");
        Directory.CreateDirectory(root);
        File.WriteAllText(Path.Combine(root, "chatgpt_phone_reg.py"), "");
        File.WriteAllText(Path.Combine(root, "runtime.json"), "{}");
        try
        {
            var paths = new ApplicationPaths(root, chosen);

            Assert.Equal(chosen, paths.ConfigDirectory);
            Assert.True(File.Exists(Path.Combine(root, "runtime.json")));
            Assert.False(File.Exists(Path.Combine(chosen, "runtime.json")));
        }
        finally
        {
            Directory.Delete(root, true);
        }
    }

    [Fact]
    public void LegacyMigrationMovesCredentialFilesOnceAndKeepsExistingTargets()
    {
        string root = Path.Combine(Path.GetTempPath(), "smsworkbench-migrate-" + Guid.NewGuid().ToString("N"));
        string source = Path.Combine(root, "repo");
        string target = Path.Combine(root, "config");
        Directory.CreateDirectory(source);
        Directory.CreateDirectory(target);
        File.WriteAllText(Path.Combine(source, "proxy.json"), "legacy-proxy");
        File.WriteAllText(Path.Combine(source, "runtime.json"), "legacy-runtime");
        File.WriteAllText(Path.Combine(source, "mailbox_tokens.txt"), "pool");
        File.WriteAllText(Path.Combine(target, "runtime.json"), "current-runtime");
        try
        {
            IReadOnlyList<string> moved = LegacyConfigMigration.Run(source, target);

            Assert.Equal(new[] { "proxy.json", "mailbox_tokens.txt" }, moved);
            Assert.False(File.Exists(Path.Combine(source, "proxy.json")));
            Assert.Equal("legacy-proxy", File.ReadAllText(Path.Combine(target, "proxy.json")));
            Assert.Equal("current-runtime", File.ReadAllText(Path.Combine(target, "runtime.json")));
            Assert.True(File.Exists(Path.Combine(source, "runtime.json")));
            Assert.Empty(LegacyConfigMigration.Run(source, target));
        }
        finally
        {
            Directory.Delete(root, true);
        }
    }
}
