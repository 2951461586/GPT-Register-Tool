// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using System.Text.Json;

namespace SmsWorkbench.Tests;

/// <summary>
/// The non-window half of a backend run: the captured stream, the output folding and
/// the task-row outcome. Split out of <c>MainWindow.Tasks.cs</c> in 2026-11-01.
/// </summary>
/// <remarks>
/// <see cref="BackendRunService.Feed"/> is called directly: the caller owns thread
/// marshalling (the window wraps it in a <see cref="Progress{T}"/>), which keeps these
/// assertions independent of any synchronisation context.
/// </remarks>
public sealed class BackendRunFoldingTests
{
    [Fact]
    public void CaptureKeepsEveryRawLineInOrder()
    {
        var run = new BackendRunService();

        run.Capture("first");
        run.Capture("second");

        Assert.Equal("first" + Environment.NewLine + "second" + Environment.NewLine, run.CapturedOutput);
    }

    [Fact]
    public void ProgressEnvelopeGoesToTheEventSinkAndNeverToThePanelOrTheCapture()
    {
        var run = new BackendRunService();
        var events = new List<BackendProgressEvent>();
        var panel = new List<string>();
        var raw = new List<string>();

        run.Feed(
            new BackendOutputLine(BackendOutputChannel.StandardOutput, Envelope("registration", "batch_completed")),
            events.Add,
            panel.Add,
            raw.Add);

        Assert.Single(events);
        Assert.Equal("batch_completed", events[0].Stage);
        Assert.Empty(raw);
        // xUnit's argument order is (expectedSubstring, actualString).
        Assert.DoesNotContain("@@SMSWORKBENCH_V2@@", run.CapturedOutput, StringComparison.Ordinal);
    }

    [Fact]
    public void AnOrdinaryLineIsCapturedAndFoldedForThePanel()
    {
        var run = new BackendRunService();
        var panel = new List<string>();
        var raw = new List<string>();

        run.Feed(
            new BackendOutputLine(BackendOutputChannel.StandardOutput, "plain backend line"),
            _ => { },
            panel.Add,
            raw.Add);

        Assert.Contains("plain backend line", run.CapturedOutput, StringComparison.Ordinal);
        Assert.Equal(new[] { "plain backend line" }, raw);
        Assert.NotEmpty(panel);
    }

    [Fact]
    public void DescribeReportsSuccessWithTheBatchSummaryWhenOneWasParsed()
    {
        var started = new DateTime(2026, 11, 1, 10, 0, 0);
        var finished = started.AddSeconds(42);
        var interpreted = new BackendExecutionResult(true, "display text", "completed", null);

        BackendRunOutcome outcome = BackendRunService.Describe(interpreted, "完成 3/3，失败 0", started, finished);

        Assert.Equal("完成", outcome.Status);
        Assert.Equal("完成 3/3，失败 0", outcome.Info);
        Assert.Equal("42", outcome.CostSeconds);
        Assert.Equal("2026-11-01 10:00:42", outcome.DoneAt);
    }

    [Fact]
    public void DescribeLeavesTheRowTextAloneWhenThereWasNoSummary()
    {
        var started = new DateTime(2026, 11, 1, 10, 0, 0);
        var interpreted = new BackendExecutionResult(false, "display text", "failed", null);

        BackendRunOutcome outcome = BackendRunService.Describe(interpreted, null, started, started);

        Assert.Equal("失败", outcome.Status);
        Assert.Null(outcome.Info);
        Assert.Equal("0", outcome.CostSeconds);
    }

    private static string Envelope(string domain, string stage)
    {
        // Serialized from dictionaries, not an anonymous type: the parser matches
        // these keys byte-for-byte, and a dictionary cannot pick up a naming policy
        // or an inferred property name by accident.
        var root = new Dictionary<string, object?>
        {
            ["version"] = 2,
            ["type"] = "event",
            ["schema"] = "smsworkbench.ipc.v2",
            ["sequence"] = 1,
            ["terminal"] = true,
            ["command_id"] = "c1",
            ["payload"] = new Dictionary<string, object?>
            {
                ["domain"] = domain,
                ["stage"] = stage,
                ["status"] = "completed",
                ["run_id"] = "r1",
                ["account_ref"] = "a@example.com",
                ["detail"] = "",
            },
        };
        return BackendProgressEventParser.Prefix + JsonSerializer.Serialize(root);
    }
}
