namespace SmsWorkbench;

/// <summary>
/// Window-independent interpreter for backend JSON results that IO-owns
/// the business-logic interpretation of every backend command family
/// (registration, liveness, payment, proxy-test, import, export).
///
/// Every method returns a sealed record so the caller (MainWindow or a
/// ViewModel) only formats the text for display — no ad-hoc JSON parsing.
///
/// This generalizes the <see cref="ProtocolPaymentResultPresenter"/> pattern
/// across all backend-command results so the JSON business logic lives in
/// exactly one module that can be unit tested without WPF.
/// </summary>
public static class BackendResultInterpreter
{
    // ── Scan (liveness) results ─────────────────────────────────────────

    /// <summary>
    /// Extracts the last JSON block containing both "results" and "total"
    /// from a raw backend output string. Returns null when no such block
    /// can be found.
    /// </summary>
    public static Dictionary<string, object>? TryExtractScanSummary(string output)
    {
        string text = output ?? "";
        int end = text.LastIndexOf('}');
        if (end < 0) return null;
        for (int start = text.LastIndexOf('{', end); start >= 0; start = start > 0 ? text.LastIndexOf('{', start - 1) : -1)
        {
            string candidate = text.Substring(start, end - start + 1);
            try
            {
                var parsed = BackendJson.TextToObject(candidate);
                if (parsed.ContainsKey("results") && parsed.ContainsKey("total"))
                    return parsed;
            }
            catch
            {
            }
        }
        return null;
    }

    /// <summary>
    /// Determines whether a row from a scan result represents a deactivated
    /// account by checking the row itself, its nested "probe", and "relogin"
    /// dictionaries.
    /// </summary>
    public static bool IsProbeDeactivated(Dictionary<string, object> row)
    {
        if (row == null) return false;
        if (IsDeactivatedMap(row)) return true;
        if (BackendJson.TryGetMap(row, "probe", out var probe) && IsDeactivatedMap(probe)) return true;
        if (BackendJson.TryGetMap(row, "relogin", out var relogin) && IsDeactivatedMap(relogin)) return true;
        return false;
    }

    /// <summary>
    /// Checks multiple known keys for deactivation-related strings.
    /// </summary>
    private static bool IsDeactivatedMap(Dictionary<string, object> data)
    {
        if (data == null) return false;
        foreach (string key in new[] { "status", "quota_status", "account_scan_status", "error", "reason" })
        {
            string value = BackendJson.GetString(data, key).Trim();
            if (value.Contains("account_deactivated", StringComparison.OrdinalIgnoreCase)
                || value.Contains("account_deatived", StringComparison.OrdinalIgnoreCase)
                || value.Equals("account_deleted", StringComparison.OrdinalIgnoreCase)
                || value.Equals("deactivated", StringComparison.OrdinalIgnoreCase)
                || value.Contains("account has been deactivated", StringComparison.OrdinalIgnoreCase)
                || value.Contains("deleted or deactivated", StringComparison.OrdinalIgnoreCase))
                return true;
        }
        return false;
    }

    /// <summary>
    /// Returns a human-readable status label for a probe result dictionary.
    /// </summary>
    public static string ProbeStatusLabel(Dictionary<string, object> probe)
    {
        if (IsDeactivatedMap(probe)) return "账号停用";
        if (BackendJson.GetString(probe, "status_code") == "401"
            || BackendJson.GetString(probe, "status").Equals("token_invalid", StringComparison.OrdinalIgnoreCase))
            return "AT失效 / HTTP 401";
        if (BackendJson.GetBool(probe, "ok"))
        {
            string statusCode = BackendJson.GetString(probe, "status_code");
            return statusCode.Length > 0 ? "AT有效 / HTTP " + statusCode : "AT有效";
        }
        string failedCode = BackendJson.GetString(probe, "status_code");
        return failedCode.Length > 0 ? "测活失败 / HTTP " + failedCode : "测活失败";
    }

    /// <summary>
    /// Returns <c>true</c> if the probe result indicates success (HTTP 200
    /// or explicit ok flag).
    /// </summary>
    public static bool IsProbeSucceeded(Dictionary<string, object> row)
    {
        if (BackendJson.TryGetMap(row, "probe", out var probe) && BackendJson.GetBool(probe, "ok"))
            return true;
        return false;
    }

    /// <summary>
    /// Returns <c>true</c> if the probe result indicates HTTP 401 /
    /// token_invalid.
    /// </summary>
    public static bool IsProbeReturned401(Dictionary<string, object> row)
    {
        if (!BackendJson.TryGetMap(row, "probe", out var probe)) return false;
        string status = BackendJson.GetString(probe, "status").Trim().ToLowerInvariant();
        return BackendJson.GetString(probe, "status_code") == "401" || status == "token_invalid";
    }

    /// <summary>
    /// Returns the first error string found in the oauth/refresh sub-sections
    /// of a scan result row.
    /// </summary>
    public static string ScanResultError(Dictionary<string, object> row)
    {
        foreach (string section in new[] { "oauth", "refresh" })
        {
            if (BackendJson.TryGetMap(row, section, out var map))
            {
                string error = BackendJson.GetString(map, "error");
                if (error.Length > 0) return error;
            }
        }
        return "";
    }

    /// <summary>
    /// Maps a canonical scan status to a Chinese label.
    /// </summary>
    public static string ScanStatusLabel(string status)
    {
        string value = (status ?? "").Trim().ToLowerInvariant();
        return value switch
        {
            "alive" => "正常",
            "alive_probe_inconclusive" => "RT正常 / OAuth深度探测未完成",
            "account_deactivated" => "账号掉号",
            "secondary_phone_verification_required" => "手机验证",
            "phone_verification_required" => "支付完成",
            "scan_failed" => "扫描失败",
            _ => value.Length > 0 ? value : "未知"
        };
    }

    // ── Proxy test results ──────────────────────────────────────────────

    /// <summary>
    /// Parses the JSON result of a proxy test and returns a structured
    /// representation of each stage.
    /// </summary>
    public static ProxyTestResult ParseProxyTestResult(string rawJson)
    {
        try
        {
            using var doc = JsonDocument.Parse(rawJson ?? "");
            JsonElement root = doc.RootElement;
            bool allOk = root.TryGetProperty("ok", out var okEl) && okEl.ValueKind == JsonValueKind.True;

            var stages = new List<ProxyTestStageResult>();
            if (root.TryGetProperty("stages", out var stagesEl) && stagesEl.ValueKind == JsonValueKind.Object)
            {
                foreach (string stage in new[] { "checkout", "approve", "update" })
                {
                    if (!stagesEl.TryGetProperty(stage, out var stageEl)) continue;
                    string ip = stageEl.TryGetProperty("ip", out var ipEl) ? ipEl.GetString() ?? "" : "";
                    string actual = stageEl.TryGetProperty("country_code", out var ccEl) ? ccEl.GetString() ?? "" : "";
                    string expected = stageEl.TryGetProperty("expected_country", out var expEl) ? expEl.GetString() ?? "" : "";
                    string error = stageEl.TryGetProperty("error", out var errEl) ? errEl.GetString() ?? "" : "";
                    stages.Add(new ProxyTestStageResult(stage, ip, actual, expected, error));
                }
            }

            return new ProxyTestResult(allOk, stages);
        }
        catch
        {
            return new ProxyTestResult(false, new List<ProxyTestStageResult>());
        }
    }

    // ── Backend execution result normalization ───────────────────────────

    /// <summary>
    /// Normalizes a <see cref="BackendCommandResult"/> into a structured
    /// presentation. Handles timed-out, cancelled, error, and successful
    /// outcomes uniformly so MainWindow doesn't repeat the same three-way
    /// catch block across every command family.
    /// </summary>
    public static BackendExecutionResult Interpret(
        BackendCommandResult result,
        string taskName,
        int? timeoutSeconds = null)
    {
        if (result.TimedOut)
            return new BackendExecutionResult(
                false,
                $"[已超时] 后端任务超时 ({(timeoutSeconds ?? 120)}s)",
                "timed_out",
                null);

        if (result.ExitCode != 0)
        {
            // The Python CLI contract distinguishes exit codes: 1 = missing
            // argument, 2 = precondition/preflight failure, 3 = runtime
            // failure. Keep the "failed" state (UI depends on it) but surface
            // the category in the message.
            string prefix = result.ExitCode == 1 ? "[失败·参数]"
                : result.ExitCode == 2 ? "[失败·前置检查]"
                : "[失败·运行时]";
            string errorText = SensitiveDataSanitizer.Redact(
                string.IsNullOrEmpty(result.StandardError) ? result.StandardOutput : result.StandardError);
            return new BackendExecutionResult(
                false,
                $"{prefix} {errorText}".TrimEnd(),
                "failed",
                result.Payload);
        }

        // Exit code 0 means success. The backend CLI deliberately writes
        // progress and diagnostics to stderr on success paths (for example
        // --view-inbox/--gmail-send redirect progress output there), so
        // stderr content alone must not flip a completed run to failure.
        if (result.Payload.HasValue)
        {
            return new BackendExecutionResult(
                true,
                result.Payload.Value.GetRawText(),
                "completed",
                result.Payload);
        }

        string output = SensitiveDataSanitizer.Redact(result.StandardOutput ?? "");
        if (output.Length == 0 && !string.IsNullOrEmpty(result.StandardError))
            output = SensitiveDataSanitizer.Redact(result.StandardError);
        return new BackendExecutionResult(
            output.Length > 0,
            output.Length > 0 ? output : "[完成] 后端任务已结束",
            "completed",
            null);
    }

    /// <summary>
    /// Creates a cancelled-task presentation.
    /// </summary>
    public static BackendExecutionResult Cancelled(string taskName)
    {
        return new BackendExecutionResult(false, "[已取消]", "cancelled", null);
    }

    /// <summary>
    /// Creates a startup-failure presentation.
    /// </summary>
    public static BackendExecutionResult StartupFailed(string taskName, string message)
    {
        return new BackendExecutionResult(false, $"[启动失败] {message}", "failed", null);
    }
}
