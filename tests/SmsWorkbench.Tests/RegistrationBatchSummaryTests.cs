// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using System.Text.Json;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

/// <summary>
/// The registration batch payload is typed now, and the type is the contract:
/// `result_schema.json` pins its properties against the keys the Python builder
/// returns, and `tests/workbench/test_result_schema_check.py` pins the same file.
/// </summary>
public sealed class RegistrationBatchSummaryTests
{
    private const string CompletePayload = """
        {
          "ok": true,
          "batch_id": "b-1",
          "total": 3,
          "success": 2,
          "failed": 1,
          "session_saved": 2,
          "db_saved": 3,
          "quality": {"deactivated": 0},
          "promotion": null,
          "funnel": {"attempted": 3},
          "health": {"queued": 1}
        }
        """;

    private static JsonElement Json(string text)
    {
        using JsonDocument document = JsonDocument.Parse(text);
        return document.RootElement.Clone();
    }

    [Fact]
    public void TryParse_MapsEveryDeclaredKey()
    {
        Assert.True(RegistrationBatchSummary.TryParse(Json(CompletePayload), out RegistrationBatchSummary? summary));
        Assert.NotNull(summary);
        Assert.True(summary.Ok);
        Assert.Equal("b-1", summary.BatchId);
        Assert.Equal(3, summary.Total);
        Assert.Equal(2, summary.Success);
        Assert.Equal(1, summary.Failed);
        Assert.Equal(2, summary.SessionSaved);
        Assert.Equal(3, summary.DbSaved);
        Assert.Equal(JsonValueKind.Object, summary.Quality?.ValueKind);
        Assert.Null(summary.Promotion);
        Assert.Equal(JsonValueKind.Object, summary.Funnel?.ValueKind);
        Assert.Equal(JsonValueKind.Object, summary.Health?.ValueKind);
        Assert.Empty(summary.MissingCounters());
    }

    [Fact]
    public void TryParse_IgnoresUnknownKeys()
    {
        // Additive-only contract: a newer backend must not break an older build.
        string payload = """{"batch_id":"b-1","total":1,"success":1,"failed":0,"future_key":42}""";

        Assert.True(RegistrationBatchSummary.TryParse(Json(payload), out RegistrationBatchSummary? summary));
        Assert.Equal(1, summary?.Total);
    }

    [Theory]
    [InlineData("[]")]
    [InlineData("\"text\"")]
    [InlineData("42")]
    public void TryParse_RejectsNonObjects(string payload)
    {
        Assert.False(RegistrationBatchSummary.TryParse(Json(payload), out RegistrationBatchSummary? summary));
        Assert.Null(summary);
    }

    [Fact]
    public void MissingCounters_ReportsEachAbsentCounter()
    {
        Assert.True(
            RegistrationBatchSummary.TryParse(Json("""{"batch_id":"b","success":1}"""), out RegistrationBatchSummary? summary));
        Assert.NotNull(summary);
        Assert.Equal(new[] { "total", "failed" }, summary.MissingCounters());
    }

    [Fact]
    public void IsRegistrationBatch_OnlyClaimsTheRegistrationFamily()
    {
        Assert.True(RegistrationBatchSummary.TryParse(Json(CompletePayload), out RegistrationBatchSummary? batch));
        Assert.True(batch?.IsRegistrationBatch);

        // The liveness scan / promotion batch / account health also carry
        // total/success/failed, but never batch_id or funnel.
        Assert.True(RegistrationBatchSummary.TryParse(Json("""{"total":5,"success":3,"failed":2}"""), out RegistrationBatchSummary? plain));
        Assert.False(plain?.IsRegistrationBatch);
    }

    [Fact]
    public void BatchSummaryLabel_ReadsTheTypedPathAndKeepsThePreviousText()
    {
        var missing = new List<string>();

        string label = BackendResultInterpreter.BatchSummaryLabel(Json(CompletePayload), missing.Add);

        Assert.Equal("完成 2/3，失败 1", label);
        Assert.Empty(missing);
    }

    [Fact]
    public void BatchSummaryLabel_ReportsAMissingCounterInsteadOfSilentlyReadingZero()
    {
        var missing = new List<string>();
        string payload = """{"batch_id":"b-1","total":4,"success":1}""";

        string label = BackendResultInterpreter.BatchSummaryLabel(Json(payload), missing.Add);

        // `failed` is still derived, exactly as before -- but the absence is no
        // longer invisible: that is the defect this callback exists for.
        Assert.Equal("完成 1/4，失败 3", label);
        Assert.Equal(new[] { "failed" }, missing);
    }

    [Fact]
    public void BatchSummaryLabel_WithoutTotalStaysEmptyAndReportsIt()
    {
        var missing = new List<string>();

        string label = BackendResultInterpreter.BatchSummaryLabel(
            Json("""{"batch_id":"b-1","success":2,"failed":1}"""),
            missing.Add);

        Assert.Equal("", label);
        Assert.Contains("total", missing);
    }

    [Fact]
    public void BatchSummaryLabel_LeavesTheOtherAccountBatchFamiliesOnTheUnionPath()
    {
        var missing = new List<string>();

        // No batch_id/funnel: this is the liveness/health shape, still read the
        // pre-DTO way, so nothing is reported as a missing registration key.
        string label = BackendResultInterpreter.BatchSummaryLabel(
            Json("""{"total":20,"success":17,"failed":3,"timed_out":2}"""),
            missing.Add);

        Assert.Equal("完成 17/20，失败 3，超时 2", label);
        Assert.Empty(missing);
    }
}
