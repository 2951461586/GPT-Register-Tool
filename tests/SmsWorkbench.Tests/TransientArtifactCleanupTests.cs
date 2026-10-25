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

    // ── RunWithCleanupAsync ─────────────────────────────────────────────
    // The desktop writes a selected-mailbox batch file (raw mailbox
    // credentials) *before* it calls the backend runner, so the cleanup has to
    // cover the paths that return before the runner's own try block -- the
    // single-task guard and a throw while the progress dialog is built. These
    // pin the wrapper's contract instead of relying on WPF code reading.

    [Fact]
    public async Task RunWithCleanupAsync_DeletesOnEarlyReturnWithoutAwaiting()
    {
        using var fixture = new TemporaryDirectory();
        string file = Path.Combine(fixture.Path, "selected_mailbox_20260101_000000.txt");
        File.WriteAllText(file, "user@example.com----password");

        await TransientArtifactCleanup.RunWithCleanupAsync(
            new[] { file },
            // Mirrors `if (backendTasks.IsRunning) return;`: the body completes
            // without touching the file list at all.
            () => Task.CompletedTask);

        Assert.False(File.Exists(file));
    }

    [Fact]
    public async Task RunWithCleanupAsync_DeletesOnSuccess()
    {
        using var fixture = new TemporaryDirectory();
        string file = Path.Combine(fixture.Path, "oneclick_sms_emails_20260101_000000_ab12cd34.txt");
        File.WriteAllText(file, "a@example.com");
        bool bodyRan = false;

        await TransientArtifactCleanup.RunWithCleanupAsync(new[] { file }, () =>
        {
            bodyRan = true;
            return Task.CompletedTask;
        });

        Assert.True(bodyRan);
        Assert.False(File.Exists(file));
    }

    [Fact]
    public async Task RunWithCleanupAsync_DeletesWhenBodyThrows()
    {
        using var fixture = new TemporaryDirectory();
        string file = Path.Combine(fixture.Path, "delete_emails_20260101_000000.txt");
        File.WriteAllText(file, "a@example.com");

        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            TransientArtifactCleanup.RunWithCleanupAsync(
                new[] { file },
                () => throw new InvalidOperationException("backend refused to start")));

        Assert.False(File.Exists(file));
    }

    [Fact]
    public async Task RunWithCleanupAsync_ToleratesNullBlankAndMissingPaths()
    {
        using var fixture = new TemporaryDirectory();
        string missing = Path.Combine(fixture.Path, "already-gone.txt");
        bool bodyRan = false;

        await TransientArtifactCleanup.RunWithCleanupAsync(null, () =>
        {
            bodyRan = true;
            return Task.CompletedTask;
        });
        await TransientArtifactCleanup.RunWithCleanupAsync(
            new[] { "", "   ", missing },
            () => Task.CompletedTask);

        Assert.True(bodyRan);
        Assert.False(File.Exists(missing));
    }

    [Fact]
    public async Task RunWithCleanupAsync_UsesTheSuppliedDeletionStrategy()
    {
        using var fixture = new TemporaryDirectory();
        string file = Path.Combine(fixture.Path, "keep.txt");
        File.WriteAllText(file, "x");
        IReadOnlyList<string>? seen = null;

        await TransientArtifactCleanup.RunWithCleanupAsync(
            new[] { file },
            () => Task.CompletedTask,
            paths => seen = paths);

        Assert.Equal(new[] { file }, seen);
        // The desktop's logging delete owns the real removal; the wrapper must
        // not delete behind its back.
        Assert.True(File.Exists(file));
    }
}
