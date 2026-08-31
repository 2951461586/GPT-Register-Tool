using System.Text.Json;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class BackendJsonProtocolTests
{
    [Fact]
    public void ExtractPayloadReadsVersionedResultEnvelope()
    {
        string output = "log line\n" + BackendJsonProtocol.Prefix
            + "{\"schema\":\"smsworkbench.ipc.v2\",\"version\":2,\"type\":\"result\",\"run_id\":\"r1\",\"sequence\":1,\"timestamp_ms\":123,\"terminal\":true,\"payload\":{\"ok\":true,\"count\":2}}";

        JsonElement? payload = BackendJsonProtocol.ExtractPayload(output);

        Assert.True(payload.HasValue);
        Assert.True(payload.Value.GetProperty("ok").GetBoolean());
        Assert.Equal(2, payload.Value.GetProperty("count").GetInt32());
    }

    [Fact]
    public void ExtractPayloadFallsBackToLegacyTrailingJson()
    {
        JsonElement? payload = BackendJsonProtocol.ExtractPayload("progress\n{\"ok\":true,\"nested\":{\"value\":3}}");

        Assert.Equal(3, payload?.GetProperty("nested").GetProperty("value").GetInt32());
    }

    [Fact]
    public void ExtractPayloadRejectsUnknownEnvelopeVersion()
    {
        string output = BackendJsonProtocol.Prefix
            + "{\"version\":3,\"type\":\"result\",\"payload\":{\"ok\":true}}";

        Assert.Null(BackendJsonProtocol.ExtractPayload(output));
    }

    [Fact]
    public void ExtractPayloadWarnsInsteadOfSilentlyDowngradingOnVersionMismatch()
    {
        // Without onWarning, a v3 envelope must still refuse to fall back to the
        // legacy parser (behaviour preserved by ExtractPayloadRejectsUnknownEnvelopeVersion).
        // This test additionally proves the mismatch is surfaced rather than swallowed.
        string output = BackendJsonProtocol.Prefix
            + "{\"version\":3,\"schema\":\"smsworkbench.ipc.v2\",\"type\":\"result\",\"payload\":{\"ok\":true}}";

        var warnings = new System.Collections.Generic.List<string>();
        JsonElement? payload = BackendJsonProtocol.ExtractPayload(output, warnings.Add);

        Assert.Null(payload);
        Assert.Single(warnings);
        Assert.Contains("smsworkbench.ipc.v2", warnings[0]);
    }

    [Fact]
    public void ExtractPayloadDoesNotWarnWhenNoEnvelopeIsPresent()
    {
        // Legacy-only output has no v2 envelope, so the mismatch path must stay silent.
        var warnings = new System.Collections.Generic.List<string>();
        JsonElement? payload = BackendJsonProtocol.ExtractPayload(
            "progress\n{\"ok\":true,\"nested\":{\"value\":3}}",
            warnings.Add);

        Assert.True(payload.HasValue);
        Assert.Empty(warnings);
    }
}
