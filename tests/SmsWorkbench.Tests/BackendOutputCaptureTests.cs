using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class BackendOutputCaptureTests
{
    [Fact]
    public void TailIsBoundedAndKeepsTheNewestText()
    {
        var capture = new BackendOutputCapture();
        capture.Append(new string('a', BackendOutputCapture.MaxTailChars));
        capture.Append("newest");

        Assert.True(capture.Text.Length <= BackendOutputCapture.MaxTailChars);
        Assert.EndsWith("newest", capture.Text);
    }

    [Fact]
    public void EnvelopeIsCapturedStructurallyAndNotKeptAsRawText()
    {
        var capture = new BackendOutputCapture();
        string envelope = BackendJsonProtocol.Prefix +
            "{\"version\":2,\"schema\":\"smsworkbench.ipc.v2\",\"type\":\"result\",\"payload\":{\"ok\":true}}";

        Assert.True(capture.TryCaptureEnvelope(envelope));

        Assert.True(capture.Payload!.Value.GetProperty("ok").GetBoolean());
        Assert.Equal("", capture.Text);
    }

    [Fact]
    public void MalformedEnvelopeStaysInTheTailForThePostExitParser()
    {
        var capture = new BackendOutputCapture();
        string broken = BackendJsonProtocol.Prefix + "{not json";

        // Mirrors PumpAsync: a rejected prefix line falls through to the tail.
        if (!capture.TryCaptureEnvelope(broken))
            capture.Append(broken);
        Assert.Contains(broken, capture.Text);
        Assert.Null(capture.Payload);
    }
}
