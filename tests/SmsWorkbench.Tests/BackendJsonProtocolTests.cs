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
}
