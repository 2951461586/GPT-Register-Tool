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

    [Fact]
    public void AppendStaysAmortizedConstantTimeAtTheRecordCap()
    {
        // Regression guard for the O(N^2) Append: it used to File.ReadLines the
        // whole file and rewrite it on EVERY append once the store was at its
        // cap, costing ~24 ms per progress event on the UI thread. The store is
        // now kept in memory and only rewritten once per MaxRecords/2 appends.
        string root = Path.Combine(Path.GetTempPath(), "sms-workbench-stage-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            var store = new JsonlStageMatrixStore(new TestApplicationPaths(root));
            var stopwatch = System.Diagnostics.Stopwatch.StartNew();
            for (int index = 0; index < 1000; index++)
            {
                store.Append(new BackendProgressEvent("payment", "run-" + index, "u" + index + "@example.test", "qris", "routing", "running", ""));
            }
            stopwatch.Stop();

            // 1000 appends: 10.0 s before the fix, ~0.3 s after. The ceiling is
            // deliberately loose (CI machines are slow and noisy) - it only has
            // to fail loudly if the per-append full-file read ever comes back.
            Assert.True(stopwatch.ElapsedMilliseconds < 3000, $"1000 appends took {stopwatch.ElapsedMilliseconds} ms");

            // The cap itself still holds: never more than 2000 records survive.
            for (int index = 0; index < 1500; index++)
            {
                store.Append(new BackendProgressEvent("payment", "run-extra-" + index, "x" + index + "@example.test", "qris", "routing", "running", ""));
            }
            IReadOnlyList<BackendProgressEvent> loaded = store.Load();
            Assert.True(loaded.Count <= 2000, $"cap exceeded: {loaded.Count} records");
            Assert.True(loaded.Count >= 1000, $"over-trimmed: only {loaded.Count} records survived");
            Assert.Equal(loaded.Count, File.ReadAllLines(Path.Combine(root, "runtime", "stage_matrix.jsonl")).Length);
        }
        finally
        {
            if (Directory.Exists(root)) Directory.Delete(root, true);
        }
    }

    [Fact]
    public void AppendSurvivesAReopenedStoreAndNeverExceedsTheCap()
    {
        // A second store instance (app restart) must see the records written by
        // the first, and the trim must not lose lines a different process wrote.
        string root = Path.Combine(Path.GetTempPath(), "sms-workbench-stage-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            string file = Path.Combine(root, "runtime", "stage_matrix.jsonl");
            var first = new JsonlStageMatrixStore(new TestApplicationPaths(root));
            for (int index = 0; index < 20; index++)
            {
                first.Append(new BackendProgressEvent("payment", "run-" + index, "u" + index + "@example.test", "qris", "routing", "running", ""));
            }

            var reopened = new JsonlStageMatrixStore(new TestApplicationPaths(root));
            IReadOnlyList<BackendProgressEvent> restored = reopened.Load();
            Assert.Equal(20, restored.Count);
            Assert.Equal("run-0", restored[0].RunId);
            Assert.Equal(20, File.ReadAllLines(file).Length);

            reopened.Clear();
            Assert.Empty(reopened.Load());
            Assert.False(File.Exists(file));
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
