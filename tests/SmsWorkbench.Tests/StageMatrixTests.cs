namespace SmsWorkbench.Tests;

public sealed class StageMatrixTests
{
    [Fact]
    public void ParserParsesVersionTwoEventAndRejectsPlainOutput()
    {
        const string line = "@@SMSWORKBENCH_V2@@{\"schema\":\"smsworkbench.ipc.v2\",\"version\":2,\"type\":\"event\",\"run_id\":\"r1\",\"sequence\":7,\"timestamp_ms\":123,\"terminal\":false,\"payload\":{\"domain\":\"registration\",\"run_id\":\"r1\",\"account_ref\":\"a@example.test\",\"stage\":\"email_otp_wait\",\"status\":\"running\",\"detail\":\"waiting\",\"attempt\":2,\"max_attempts\":3,\"country\":\"US\",\"total\":12}}";

        Assert.True(BackendProgressEventParser.TryParse(line, out BackendProgressEvent value));
        Assert.Equal("registration", value.Domain);
        Assert.Equal("email_otp_wait", value.Stage);
        Assert.Equal(2, value.Attempt);
        Assert.Equal(7, value.Sequence);
        Assert.Equal(12, value.Total);
        Assert.False(BackendProgressEventParser.TryParse("ordinary backend output", out _));
    }

    [Fact]
    public void AccountBatchProgressTrackerCountsUniqueTerminalAccounts()
    {
        var tracker = new AccountBatchProgressTracker("account_scan", 3);

        tracker.Update(new BackendProgressEvent("account_scan", "run-1", "a@example.test", "", "account_completed", "completed", "active", Terminal: true, Total: 3));
        tracker.Update(new BackendProgressEvent("account_scan", "run-1", "A@example.test", "", "account_completed", "failed", "retry", Terminal: true, Total: 3));
        tracker.Update(new BackendProgressEvent("account_scan", "run-1", "b@example.test", "", "probing", "running", "", Terminal: false, Total: 3));
        tracker.Update(new BackendProgressEvent("account_promotion", "run-2", "c@example.test", "", "account_completed", "completed", "", Terminal: true, Total: 5));

        Assert.Equal(1, tracker.Completed);
        Assert.Equal(3, tracker.Total);
    }

    [Fact]
    public void ViewModel_ConsolidatesAccountStagesAndTracksCompletion()
    {
        var viewModel = new StageMatrixViewModel();
        viewModel.Apply(new BackendProgressEvent("payment", "run-1", "a@example.test", "qris", "routing", "running", ""));
        viewModel.Apply(new BackendProgressEvent("payment", "run-1", "a@example.test", "qris", "completed", "completed", "done"));

        StageMatrixRun run = Assert.Single(viewModel.Runs);
        Assert.Equal("completed", run.Status);
        Assert.Equal("qris", run.Method);
        Assert.Contains(run.Cells, cell => cell.Stage == "routing");
        Assert.Contains(run.Cells, cell => cell.Status == "completed");
    }

    [Fact]
    public void Parser_UsesExecutorStateAndMessageFallbacks()
    {
        const string line = "@@SMSWORKBENCH_V2@@{\"schema\":\"smsworkbench.ipc.v2\",\"version\":2,\"type\":\"event\",\"run_id\":\"p1\",\"sequence\":1,\"timestamp_ms\":123,\"terminal\":false,\"payload\":{\"domain\":\"payment\",\"run_id\":\"p1\",\"method\":\"bizum\",\"stage\":\"routing\",\"state\":\"preparing_proxy\",\"message\":\"payment routes prepared\"}}";

        Assert.True(BackendProgressEventParser.TryParse(line, out BackendProgressEvent value));
        Assert.Equal("preparing_proxy", value.Status);
        Assert.Equal("payment routes prepared", value.Detail);
    }

    [Fact]
    public void ViewModel_UsesRunIdSoRepeatedAccountRunsStaySeparate()
    {
        var viewModel = new StageMatrixViewModel();
        viewModel.Apply(new BackendProgressEvent("payment", "run-1", "same@example.test", "qris", "routing", "running", ""));
        viewModel.Apply(new BackendProgressEvent("payment", "run-2", "same@example.test", "qris", "routing", "running", ""));

        Assert.Equal(2, viewModel.Runs.Count);
    }

    [Fact]
    public void StoreReloadsAndRedactsAccountReference()
    {
        string root = Path.Combine(Path.GetTempPath(), "sms-workbench-stage-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            var store = new JsonlStageMatrixStore(new TestApplicationPaths(root));
            store.Append(new BackendProgressEvent("registration", "run-1", "secret@example.test", "", "started", "running", ""));
            var restored = new StageMatrixViewModel(store);
            StageMatrixRun run = Assert.Single(restored.Runs);
            Assert.StartsWith("account-", run.AccountRef);
            Assert.DoesNotContain("secret@example.test", File.ReadAllText(Path.Combine(root, "runtime", "stage_matrix.jsonl")));
        }
        finally
        {
            if (Directory.Exists(root)) Directory.Delete(root, true);
        }
    }

    [Theory]
    [InlineData("remail", "user@outlook.com", "remail/outlook")]
    [InlineData("icloud_url", "user@icloud.com", "icloud")]
    [InlineData("cf_worker", "user@example.com", "cfworker")]
    public void MailboxTypeDisplayDoesNotExposeSqlitePrefix(string provider, string email, string expected)
    {
        Assert.Equal(expected, MainWindow.MailboxTypeDisplay(provider, email));
    }
}
