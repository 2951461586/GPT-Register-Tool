using System.Text.Json;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

/// <summary>
/// End-to-end proof for the 账号测活 / 账号优惠检测 panel story: feed one
/// realistic desktop-mode stdout through exactly the three pieces MainWindow
/// chains together (progress-event parse → panel line, raw line → folder/fold,
/// terminal result → summary) and assert what the operator actually sees.
///
/// Each piece already has unit tests; this guards the seam between them, which
/// is where both bugs lived (events swallowed before the panel, and the result
/// envelope never unwrapped).
/// </summary>
public sealed class ScanResultPanelStoryTests
{
    private const string Prefix = "@@SMSWORKBENCH_V2@@";

    private static (List<string> Panel, string? Result) RenderDesktopRun(IEnumerable<string> rawLines)
    {
        var panel = new List<string>();
        var folder = new BackendLogFolder();
        string? result = null;
        foreach (string line in rawLines)
        {
            if (BackendProgressEventParser.TryParse(line, out BackendProgressEvent? progressEvent))
            {
                string? stageLine = BackendLogPresenter.ProgressEventLine(progressEvent);
                if (stageLine != null) panel.Add(stageLine);
                continue;
            }
            panel.AddRange(folder.Feed(line));
        }
        string all = string.Join("\n", rawLines);
        var summary = BackendResultInterpreter.TryExtractScanSummary(all);
        result = summary == null ? null : BackendJson.GetString(summary, "total");
        return (panel, result);
    }

    private static string Event(string domain, string stage, string status, int total, string detail)
    {
        string payload = JsonSerializer.Serialize(new
        {
            domain,
            run_id = "run-1",
            account_ref = "",
            stage,
            status,
            detail,
            total,
        });
        string envelope = JsonSerializer.Serialize(new
        {
            schema = "smsworkbench.ipc.v2",
            version = 2,
            type = "event",
            run_id = "run-1",
            sequence = 1,
            timestamp_ms = 0L,
            terminal = false,
            payload = JsonSerializer.Deserialize<JsonElement>(payload),
        });
        return Prefix + envelope;
    }

    private static string Result(string payloadJson)
        => Prefix + JsonSerializer.Serialize(new
        {
            schema = "smsworkbench.ipc.v2",
            version = 2,
            type = "result",
            run_id = "run-1",
            sequence = 2,
            timestamp_ms = 1L,
            terminal = true,
            payload = JsonSerializer.Deserialize<JsonElement>(payloadJson),
        });

    [Fact]
    public void LivenessRun_ShowsStageHeadersFailureLinesAndParsesTotal()
    {
        var run = new[]
        {
            Event("account_scan", "batch_started", "running", 3, "账号测活开始"),
            Event("account_scan", "account_completed", "failed", 3, "检测失败"),
            Event("account_scan", "batch_completed", "completed", 3, "正常 1/3，AT失效 1，掉号 1，超时 0"),
            "[*] 测活完成：共 3 个账号，正常 1，掉号 1，其他失败 1",
            "[!] b@example.com: AT 失效（HTTP 401）",
            "[!] c@example.com: 账号已注销",
            Result("""{"ok":false,"total":3,"success":1,"failed":2,"results":[{"email":"a@example.com","ok":true},{"email":"b@example.com","ok":false},{"email":"c@example.com","ok":false}]}"""),
        };

        (List<string> panel, string? total) = RenderDesktopRun(run);

        Assert.Equal("3", total);
        Assert.Contains("── 账号测活开始 · 共 3 个账号 ──", panel);
        Assert.Contains("── 账号测活结束 · 正常 1/3，AT失效 1，掉号 1，超时 0 ──", panel);
        Assert.Contains("[!] b@example.com: AT 失效（HTTP 401）", panel);
        // Per-account events must NOT duplicate the Python failure lines.
        Assert.DoesNotContain(panel, l => l.Contains("账号测活开始") && l.Contains("b@example.com"));
        // No raw English reason survives into the panel.
        Assert.DoesNotContain(panel, l => l.Contains("token_invalid"));
        Assert.DoesNotContain(panel, l => l.Contains("Liveness check"));
    }

    [Fact]
    public void PromotionRun_ShowsStageHeadersAndParsesTotal()
    {
        var run = new[]
        {
            Event("account_promotion", "batch_started", "running", 2, "账号优惠检测开始"),
            Event("account_promotion", "account_completed", "failed", 2, "AT失效"),
            Event("account_promotion", "batch_completed", "completed", 2, "完成 2 个账号，成功 1，401 1，传输失败 0"),
            "[*] 优惠检测完成：共 2 个账号，检测成功 1，失败 1",
            "[!] b@example.com: AT 失效（HTTP 401）",
            Result("""{"ok":false,"total":2,"success":1,"failed":1,"results":[{"email":"a@example.com","ok":true,"promotion_status":"Free·无优惠"},{"email":"b@example.com","ok":false,"promotion_status":"AT失效"}]}"""),
        };

        (List<string> panel, string? total) = RenderDesktopRun(run);

        Assert.Equal("2", total);
        Assert.Contains("── 账号优惠检测开始 · 共 2 个账号 ──", panel);
        Assert.Contains("── 账号优惠检测结束 · 完成 2 个账号，成功 1，401 1，传输失败 0 ──", panel);
        Assert.Contains("[!] b@example.com: AT 失效（HTTP 401）", panel);
    }

    [Fact]
    public void PromotionRows_AreRecognisedForTheDialog()
    {
        // The dialog renders promotion rows with their own badge, not the
        // liveness "AT有效 / HTTP 200" label.
        var summary = BackendResultInterpreter.TryExtractScanSummary(
            Result("""{"total":1,"results":[{"email":"a@example.com","ok":true,"promotion_status":"可试用Plus-50%","probe":{"ok":true,"status_code":"200"}}]}"""));
        Assert.NotNull(summary);

        var rows = new List<Dictionary<string, object>>();
        if (summary.TryGetValue("results", out object? raw) && raw is List<object> items)
        {
            foreach (object item in items)
            {
                if (item is Dictionary<string, object> map) rows.Add(map);
            }
        }

        Assert.True(BackendResultInterpreter.IsPromotionRows(rows));
        Assert.Equal("可试用Plus-50%", BackendResultInterpreter.ResultRowStatus(rows[0]));
    }
}
