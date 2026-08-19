namespace SmsWorkbench.Tests;

public sealed class StageMatrixTests
{
    [Fact]
    public void Parser_ParsesVersionOneEventAndRejectsPlainOutput()
    {
        const string line = "@@SMSWORKBENCH_EVENT_V1@@{\"version\":1,\"type\":\"event\",\"payload\":{\"domain\":\"registration\",\"run_id\":\"r1\",\"account_ref\":\"a@example.test\",\"stage\":\"email_otp_wait\",\"status\":\"running\",\"detail\":\"waiting\",\"attempt\":2,\"max_attempts\":3,\"country\":\"US\"}}";

        Assert.True(BackendProgressEventParser.TryParse(line, out BackendProgressEvent value));
        Assert.Equal("registration", value.Domain);
        Assert.Equal("email_otp_wait", value.Stage);
        Assert.Equal(2, value.Attempt);
        Assert.False(BackendProgressEventParser.TryParse("ordinary backend output", out _));
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
        const string line = "@@SMSWORKBENCH_EVENT_V1@@{\"version\":1,\"type\":\"event\",\"payload\":{\"domain\":\"payment\",\"run_id\":\"p1\",\"method\":\"bizum\",\"stage\":\"routing\",\"state\":\"preparing_proxy\",\"message\":\"payment routes prepared\"}}";

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
}
