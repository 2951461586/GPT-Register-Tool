// Explicit even though SmsWorkbench.Tests already sees SmsWorkbench through its
// enclosing namespace: the alias makes the resolution obvious to a reader (and to
// analyzers that evaluate the file outside the project's global usings).
using SmsWorkbench.Localization;

namespace SmsWorkbench.Tests;

/// <summary>
/// The rolling window behind the status-bar strip. Every case passes an explicit
/// <c>nowUtc</c>, so the arithmetic is tested without sleeping.
/// </summary>
/// <remarks>
/// The class pins the UI language to English for its lifetime: the summary line is a
/// localized string, so asserting on it in whatever language the previous test left
/// behind would make the suite order-dependent. <c>Dispose</c> restores Chinese, which
/// is the authored default.
/// </remarks>
public sealed class MetricsViewModelTests : IDisposable
{
    private static readonly DateTime Now = new(2026, 10, 28, 12, 0, 0, DateTimeKind.Utc);

    public MetricsViewModelTests() => Loc.Instance.SetLanguage(AppLanguage.En);

    public void Dispose() => Loc.Instance.SetLanguage(AppLanguage.Zh);

    private static BackendProgressEvent Terminal(string status, long durationMs = 0, string failureClass = "")
        => new(
            "registration",
            "run",
            "acct",
            "",
            "completed",
            status,
            "",
            Terminal: true,
            DurationMs: durationMs,
            FailureClass: failureClass);

    [Fact]
    public void NonTerminalAndCancelledEventsAreIgnored()
    {
        var metrics = new MetricsViewModel();

        metrics.Record(new BackendProgressEvent("registration", "run", "a", "", "user_register", "running", ""));
        metrics.Record(Terminal("cancelled"));
        metrics.Sample(Now);

        Assert.Equal(0, metrics.AttemptsPerMinute);
        // A cancelled account is not a failure: cancelling must not make the remaining
        // accounts look less reliable.
        Assert.Contains("Success -", metrics.SummaryText);
    }

    [Fact]
    public void SuccessRateCountsOnlyFinishedAttempts()
    {
        var metrics = new MetricsViewModel();
        metrics.Record(Terminal("success"), Now);
        metrics.Record(Terminal("success"), Now);
        metrics.Record(Terminal("failed"), Now);
        metrics.Sample(Now);

        Assert.Equal(3, metrics.AttemptsPerMinute);
        Assert.Contains("Success 67%", metrics.SummaryText);
    }

    [Fact]
    public void RateWindowDropsAttemptsOlderThanAMinute()
    {
        var metrics = new MetricsViewModel();
        metrics.Record(Terminal("success"), Now.AddSeconds(-90));
        metrics.Record(Terminal("success"), Now.AddSeconds(-30));
        metrics.Sample(Now);

        Assert.Equal(1, metrics.AttemptsPerMinute);
    }

    [Fact]
    public void HistoryIsSixtyBucketsOldestFirst()
    {
        var metrics = new MetricsViewModel();
        Assert.Equal(MetricsViewModel.BucketCount, metrics.Throughput.Count);

        // Now lands in the right-most bucket; ten seconds ago in the one before it.
        metrics.Record(Terminal("success"), Now);
        metrics.Record(Terminal("success"), Now.AddSeconds(-10));
        metrics.Sample(Now);

        Assert.Equal(1.0, metrics.Throughput[MetricsViewModel.BucketCount - 1]);
        Assert.Equal(1.0, metrics.Throughput[MetricsViewModel.BucketCount - 2]);
        Assert.Equal(0.0, metrics.Throughput[0]);
    }

    [Fact]
    public void SamplesOlderThanTheWindowAreDropped()
    {
        var metrics = new MetricsViewModel();
        metrics.Record(Terminal("success"), Now.AddMinutes(-30));
        metrics.Sample(Now);

        Assert.Equal(0, metrics.AttemptsPerMinute);
        Assert.All(metrics.Throughput, value => Assert.Equal(0.0, value));
    }

    [Fact]
    public void AverageDurationUsesOnlyMeasuredRuns()
    {
        var metrics = new MetricsViewModel();
        metrics.Record(Terminal("success", 20_000), Now);
        metrics.Record(Terminal("failed", 40_000), Now);
        metrics.Record(Terminal("success", 0), Now);
        metrics.Sample(Now);

        Assert.Contains("Avg 30.0s", metrics.SummaryText);
    }

    [Fact]
    public void FailureSplitGroupsByClassMostCommonFirst()
    {
        var metrics = new MetricsViewModel();
        metrics.Record(Terminal("failed", failureClass: "otp_timeout"), Now);
        metrics.Record(Terminal("failed", failureClass: "otp_timeout"), Now);
        metrics.Record(Terminal("failed", failureClass: "auth_state"), Now);
        metrics.Record(Terminal("success"), Now);
        metrics.Sample(Now);

        Assert.True(metrics.FailureVisible);
        Assert.Equal(
            new[] { new FailureSlice("otp_timeout", 2), new FailureSlice("auth_state", 1) },
            metrics.Failures);
        Assert.Contains("Failed 3", metrics.FailureText);
        Assert.Contains("otp_timeout 2", metrics.FailureText);
    }

    [Fact]
    public void FailureClassThatIsMissingCollapsesIntoOneBucket()
    {
        var metrics = new MetricsViewModel();
        metrics.Record(Terminal("failed"), Now);
        metrics.Record(Terminal("failed", failureClass: ""), Now);
        metrics.Sample(Now);

        FailureSlice only = Assert.Single(metrics.Failures);
        Assert.Equal(2, only.Count);
        // Localized bucket, not a blank label; assert the catalog value rather than
        // hard-coding its capitalisation.
        Assert.Equal(SmsWorkbench.Localization.Loc.T("未知"), only.FailureClass);
    }

    [Fact]
    public void FailureSlotDisappearsWhenTheWindowEmpties()
    {
        var metrics = new MetricsViewModel();
        metrics.Record(Terminal("failed", failureClass: "otp_timeout"), Now);
        metrics.Sample(Now);
        Assert.True(metrics.FailureVisible);

        // Ten minutes later the failure has aged out of the window.
        metrics.Sample(Now.AddMinutes(11));

        Assert.False(metrics.FailureVisible);
        Assert.Empty(metrics.Failures);
        Assert.Equal("", metrics.FailureText);
    }

    [Theory]
    [InlineData(450, "450ms")]
    [InlineData(12_300, "12.3s")]
    [InlineData(120_000, "2.0m")]
    public void DurationFormattingStaysShort(double milliseconds, string expected)
        => Assert.Equal(expected, MetricsViewModel.FormatDuration(milliseconds));

    [Fact]
    public void SummaryFollowsTheLanguage()
    {
        var metrics = new MetricsViewModel();
        // Real clock on purpose: RefreshLanguage() re-samples against DateTime.UtcNow, so
        // anchoring this attempt at the fixed `Now` would make the test pass only during
        // the ten minutes after that timestamp.
        metrics.Record(Terminal("success"), DateTime.UtcNow);

        Loc.Instance.SetLanguage(AppLanguage.Zh);
        metrics.RefreshLanguage();
        Assert.Contains("吞吐 1/分", metrics.SummaryText);

        Loc.Instance.SetLanguage(AppLanguage.En);
        metrics.RefreshLanguage();
        Assert.Contains("Throughput 1/min", metrics.SummaryText);
    }
}
