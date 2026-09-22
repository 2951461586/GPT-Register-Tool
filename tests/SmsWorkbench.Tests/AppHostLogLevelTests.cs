using Serilog;
using Serilog.Core;
using Serilog.Events;

namespace SmsWorkbench.Tests;

/// <summary>
/// The panel log (<c>MainWindow.UiLog</c>) writes at <c>Debug</c>, below the
/// file sink's <c>Information</c> floor, so panel content never reached
/// <c>runtime/app_*.log</c> and offline replay was the only way to read it.
/// <c>SMSWORKBENCH_LOG_LEVEL</c> is the gate that persists it; the default
/// must stay <c>Information</c>.
/// </summary>
public class AppHostLogLevelTests : IDisposable
{
    private readonly string? _original =
        Environment.GetEnvironmentVariable("SMSWORKBENCH_LOG_LEVEL");

    public void Dispose()
    {
        Environment.SetEnvironmentVariable("SMSWORKBENCH_LOG_LEVEL", _original);
        GC.SuppressFinalize(this);
    }

    [Fact]
    public void UnsetKeepsThePreviousFloor()
    {
        Environment.SetEnvironmentVariable("SMSWORKBENCH_LOG_LEVEL", null);
        Assert.Equal(LogEventLevel.Information, AppHost.ResolveMinimumLevel());
    }

    [Theory]
    [InlineData("", LogEventLevel.Information)]
    [InlineData("   ", LogEventLevel.Information)]
    [InlineData("nonsense", LogEventLevel.Information)]
    [InlineData("Debug", LogEventLevel.Debug)]
    [InlineData("debug", LogEventLevel.Debug)]
    [InlineData("  Debug  ", LogEventLevel.Debug)]
    [InlineData("Verbose", LogEventLevel.Verbose)]
    [InlineData("Warning", LogEventLevel.Warning)]
    public void TheEnvironmentVariableSelectsTheFloor(string raw, LogEventLevel expected)
    {
        Environment.SetEnvironmentVariable("SMSWORKBENCH_LOG_LEVEL", raw);
        Assert.Equal(expected, AppHost.ResolveMinimumLevel());
    }

    [Fact]
    public void DebugActuallySitsBelowInformation()
    {
        // Precondition for the gate to do anything at all: if Debug were not
        // below Information, ResolveMinimumLevel returning Debug would still
        // filter every panel line and the env var would be a no-op.  Asserting
        // the premise keeps the other tests from passing vacuously.
        Assert.True(LogEventLevel.Debug < LogEventLevel.Information);

        Environment.SetEnvironmentVariable("SMSWORKBENCH_LOG_LEVEL", "Debug");
        Assert.True(AppHost.ResolveMinimumLevel() < LogEventLevel.Information);
    }

    [Fact]
    public void TheGateIsWhatLetsAPanelLineThrough()
    {
        // Exercises the mechanism the gate controls rather than the parse: a
        // panel line is ``logger.Debug``, so it only survives at a Debug floor.
        Environment.SetEnvironmentVariable("SMSWORKBENCH_LOG_LEVEL", "Debug");
        Assert.Single(CaptureDebugLine());

        Environment.SetEnvironmentVariable("SMSWORKBENCH_LOG_LEVEL", null);
        Assert.Empty(CaptureDebugLine());
    }

    private static List<LogEvent> CaptureDebugLine()
    {
        var events = new List<LogEvent>();
        using (var logger = new LoggerConfiguration()
                   .MinimumLevel.Is(AppHost.ResolveMinimumLevel())
                   .WriteTo.Sink(new CollectingSink(events))
                   .CreateLogger())
        {
            logger.Debug("[backend] {Line}", "panel");
        }

        return events;
    }

    private sealed class CollectingSink : ILogEventSink
    {
        private readonly List<LogEvent> _events;

        public CollectingSink(List<LogEvent> events) => _events = events;

        public void Emit(LogEvent logEvent) => _events.Add(logEvent);
    }
}
