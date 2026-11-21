using System.Text.Json;
using SmsWorkbench.Localization;

namespace SmsWorkbench.Services
{
    public static class PaymentBatchReportParser
    {
        public static string FormatProxyProbe(JsonElement report)
        {
            bool ok = report.TryGetProperty("ok", out JsonElement okElement)
                && okElement.ValueKind == JsonValueKind.True;
            var parts = new List<string>();
            if (report.TryGetProperty("stages", out JsonElement stages)
                && stages.ValueKind == JsonValueKind.Object)
            {
                foreach (JsonProperty stage in stages.EnumerateObject())
                {
                    JsonElement value = stage.Value;
                    bool stageOk = value.TryGetProperty("ok", out JsonElement stageOkElement)
                        && stageOkElement.ValueKind == JsonValueKind.True;
                    string cc = JsonString(value, "country_code");
                    string region = JsonString(value, "region");
                    string ip = JsonString(value, "ip");
                    string error = JsonString(value, "error");
                    string where = string.Join("/", new[] { cc, region }.Where(item => item.Length > 0));
                    parts.Add(stageOk
                        ? $"{stage.Name}✓ {where} {ip}".Trim()
                        : $"{stage.Name}✗ {error}".Trim());
                }
            }
            string prefix = ok ? Loc.T("代理探测通过：") : Loc.T("代理探测存在问题：");
            return parts.Count > 0 ? prefix + string.Join("  |  ", parts) : prefix + Loc.T("无可探测的代理");
        }

        public static string PaymentAccountRef(string value)
        {
            byte[] hash = System.Security.Cryptography.SHA256.HashData(
                Encoding.UTF8.GetBytes((value ?? "").Trim().ToLowerInvariant()));
            return Convert.ToHexString(hash).ToLowerInvariant()[..16];
        }

        public static int PaymentStageProgress(string stage, bool terminal, string? method = null)
        {
            if (terminal) return 100;
            string normalized = (stage ?? "").Trim().ToLowerInvariant();
            IReadOnlyList<string> stages = Array.Empty<string>();
            try { stages = PaymentMethods.Find(method).Stages ?? Array.Empty<string>(); } catch { }
            if (stages.Count == 0)
                stages = new[] { "routing", "auth_gate", "checkout", "stripe_init", "provider", "confirm", "redirect", "artifact" };
            int index = Array.FindIndex(
                stages.ToArray(),
                item => item == normalized
                    || (normalized == "checkout_create" && item == "checkout")
                    || (normalized == "capability_probe" && item == "stripe_init")
                    || (normalized == "payment_method" && item == "provider"));
            return index < 0 ? 5 : Math.Clamp((index + 1) * 95 / stages.Count, 5, 95);
        }

        public static string PaymentStageLabel(string stage) => (stage ?? "").Trim().ToLowerInvariant() switch
        {
            "routing" => Loc.T("路由准备"),
            "auth_gate" => Loc.T("AT 验证"),
            "checkout" or "checkout_create" => Loc.T("创建 Checkout"),
            "stripe_init" or "capability_probe" => Loc.T("能力探测"),
            "provider" or "payment_method" => Loc.T("支付方式处理"),
            "approve" or "confirm" => Loc.T("支付确认"),
            "redirect" or "promotion" => Loc.T("结果确认"),
            "completed" => Loc.T("完成"),
            _ => string.IsNullOrWhiteSpace(stage) ? Loc.T("执行中") : stage,
        };

        public static IReadOnlyList<PaymentBatchResultRow> ParseResults(
            JsonElement report, Func<string, string> resolveAccountDisplay)
        {
            if (!report.TryGetProperty("results", out JsonElement values) || values.ValueKind != JsonValueKind.Array)
                return Array.Empty<PaymentBatchResultRow>();
            var parsed = new List<PaymentBatchResultRow>();
            foreach (JsonElement row in values.EnumerateArray())
            {
                string eligibility = Loc.T("未知");
                if (row.TryGetProperty("eligible", out JsonElement eligible)
                    && eligible.ValueKind is JsonValueKind.True or JsonValueKind.False)
                    eligibility = eligible.GetBoolean() ? Loc.T("符合") : Loc.T("不符合");
                string decision = JsonString(row, "decision");
                string paymentUrl = FirstNonEmpty(JsonString(row, "url"), JsonString(row, "long_url"));
                string qrData = JsonString(row, "qr_data");
                string qrPath = JsonString(row, "qr_path");
                bool paymentUrlPresent = paymentUrl.Length > 0
                    || JsonBool(row, "url_present")
                    || JsonBool(row, "long_url_present");
                bool qrDataPresent = qrData.Length > 0 || JsonBool(row, "qr_data_present");
                bool qrPathPresent = qrPath.Length > 0 || JsonBool(row, "qr_path_present");
                string terminalState = FirstNonEmpty(
                    JsonString(row, "terminal_state"),
                    JsonString(row, "status"),
                    JsonString(row, "state"));
                if (terminalState.Equals("canceled", StringComparison.OrdinalIgnoreCase))
                    terminalState = "cancelled";
                string resultKind = paymentUrlPresent
                    ? Loc.T("支付链接")
                    : qrDataPresent
                        ? Loc.T("二维码内容")
                        : qrPathPresent ? Loc.T("二维码文件") : "";
                string resultValue = FirstNonEmpty(paymentUrl, qrData, qrPath);
                string accountDisplay = resolveAccountDisplay(JsonString(row, "account_ref"));
                parsed.Add(new PaymentBatchResultRow
                {
                    AccountRef = accountDisplay,
                    MatrixCell = JsonString(row, "matrix_cell"),
                    AuthStatus = JsonBool(row, "authenticated") ? "200" : Loc.T("失败"),
                    RefreshStatus = JsonBool(row, "refreshed") ? Loc.T("已刷新") : Loc.T("未刷新"),
                    Eligibility = eligibility,
                    Decision = decision.Length > 0 ? decision : JsonString(row, "error"),
                    TerminalState = terminalState,
                    ErrorStage = JsonString(row, "error_stage"),
                    Retryable = JsonBool(row, "retryable"),
                    ResultKind = resultKind,
                    ResultValue = resultValue,
                    ResultPresent = paymentUrlPresent || qrDataPresent || qrPathPresent,
                    AuthorizationQueued = JsonBool(row, "authorization_queued"),
                    AuthorizationStatus = JsonString(row, "authorization_status"),
                    ProgressPercent = 100,
                    ProgressText = "100%",
                    CurrentStage = Loc.T("完成"),
                    ResultStatus = JsonBool(row, "ok")
                        || terminalState.Equals("completed", StringComparison.OrdinalIgnoreCase)
                        || paymentUrlPresent || qrDataPresent || qrPathPresent
                        ? Loc.T("成功")
                        : "失败",
                    Attempts = JsonInt(row, "attempts")
                });
            }
            return parsed;
        }

        public static string FormatSummary(JsonElement report)
        {
            if (!report.TryGetProperty("counts", out JsonElement counts) || counts.ValueKind != JsonValueKind.Object)
                return Loc.T("批次已结束，但未返回计数。");
            return Loc.F("请求 {0}  ·  AT 200 {1}", JsonInt(counts, "requested"), JsonInt(counts, "authenticated"))
                + Loc.F("  ·  JIT {0}  ·  资格 {1}", JsonInt(counts, "refreshed"), JsonInt(counts, "eligible"))
                + Loc.F("  ·  完成 {0}  ·  链接 {1}", JsonInt(counts, "completed"), JsonInt(counts, "link_ready"))
                + Loc.F("  ·  二维码 {0}  ·  取消 {1}", JsonInt(counts, "qr_ready"), JsonInt(counts, "cancelled"))
                + Loc.F("  ·  未知 {0}  ·  超时 {1}", JsonInt(counts, "unknown"), JsonInt(counts, "timed_out"))
                + Loc.F("  ·  失败 {0}  ·  可重试 {1}", JsonInt(counts, "failed"), JsonInt(counts, "retryable"))
                + Loc.F("  ·  断点恢复 {0}", JsonInt(report, "resumed"));
        }

        public static string JsonString(JsonElement element, string name)
        {
            if (!element.TryGetProperty(name, out JsonElement value)) return "";
            return value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : value.ToString();
        }

        public static int JsonInt(JsonElement element, string name)
        {
            if (!element.TryGetProperty(name, out JsonElement value)) return 0;
            if (value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out int number)) return number;
            return int.TryParse(value.ToString(), out number) ? number : 0;
        }

        public static bool JsonBool(JsonElement element, string name)
            => element.TryGetProperty(name, out JsonElement value) && value.ValueKind == JsonValueKind.True;

        public static string FirstNonEmpty(params string[] values)
            => values.FirstOrDefault(value => !string.IsNullOrWhiteSpace(value))?.Trim() ?? "";
    }
}
