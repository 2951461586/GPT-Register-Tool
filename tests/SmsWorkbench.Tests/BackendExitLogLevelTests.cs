using Serilog.Events;

namespace SmsWorkbench.Tests;

public class BackendExitLogLevelTests
{
    [Theory]
    [InlineData(0, true, false, LogEventLevel.Information)]
    [InlineData(0, false, false, LogEventLevel.Warning)]
    [InlineData(3, true, false, LogEventLevel.Warning)]
    [InlineData(3, false, false, LogEventLevel.Warning)]
    [InlineData(-1, false, false, LogEventLevel.Error)]
    [InlineData(0, true, true, LogEventLevel.Error)]
    public void ExitOutcomeSelectsSeverity(int exitCode, bool payload, bool timedOut, LogEventLevel expected)
    {
        Assert.Equal(expected, PythonBackendClient.ExitLogLevel(exitCode, payload, timedOut));
    }
}
