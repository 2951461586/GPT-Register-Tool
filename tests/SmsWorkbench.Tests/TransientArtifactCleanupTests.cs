using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class TransientArtifactCleanupTests
{
    [Fact]
    public void DeletesLeftoverAccessTokenFilesAndKeepsEverythingElse()
    {
        using var fixture = new TemporaryDirectory();
        string tmp = Path.Combine(fixture.Path, "runtime", "tmp");
        Directory.CreateDirectory(tmp);
        string first = Path.Combine(tmp, "protocol_payment_at_0123456789abcdef.json");
        string second = Path.Combine(tmp, "protocol_payment_at_fedcba9876543210.json");
        string unrelated = Path.Combine(tmp, "desktop_read_cache.json");
        File.WriteAllText(first, "{\"access_token\":\"secret\"}");
        File.WriteAllText(second, "{\"access_token\":\"secret\"}");
        File.WriteAllText(unrelated, "{}");

        int deleted = TransientArtifactCleanup.DeletePaymentAccessTokenFiles(fixture.Path);

        Assert.Equal(2, deleted);
        Assert.False(File.Exists(first));
        Assert.False(File.Exists(second));
        Assert.True(File.Exists(unrelated));
    }

    [Fact]
    public void MissingRuntimeTmpDirectoryIsNotCreatedAndDeletesNothing()
    {
        using var fixture = new TemporaryDirectory();

        int deleted = TransientArtifactCleanup.DeletePaymentAccessTokenFiles(fixture.Path);

        Assert.Equal(0, deleted);
        Assert.False(Directory.Exists(Path.Combine(fixture.Path, "runtime", "tmp")));
    }

    [Fact]
    public void BlankRootDirectoryIsIgnored()
    {
        Assert.Equal(0, TransientArtifactCleanup.DeletePaymentAccessTokenFiles(""));
        Assert.Equal(0, TransientArtifactCleanup.DeletePaymentAccessTokenFiles("   "));
    }
}
