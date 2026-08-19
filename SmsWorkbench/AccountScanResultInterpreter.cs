namespace SmsWorkbench
{
    /// <summary>
    /// Window-independent interpretation of account liveness (测活) output:
    /// the trailing scan-summary JSON object printed by the backend, the
    /// per-row probe/relogin classification, and the single-account
    /// quota-usage probe detail. Pure functions over <see cref="BackendJson"/>
    /// dictionaries so they can be unit tested without WPF.
    /// </summary>
    public static class AccountScanResultInterpreter
    {
        /// <summary>
        /// Scans backwards through backend stdout for the last balanced JSON
        /// object that looks like a scan summary (contains "results" and
        /// "total").
        /// </summary>
        public static bool TryExtractScanSummary(string output, out Dictionary<string, object> summary)
        {
            summary = null;
            string text = output ?? "";
            int end = text.LastIndexOf('}');
            if (end < 0) return false;
            for (int start = text.LastIndexOf('{', end); start >= 0; start = start > 0 ? text.LastIndexOf('{', start - 1) : -1)
            {
                string candidate = text.Substring(start, end - start + 1);
                try
                {
                    var parsed = BackendJson.TextToObject(candidate);
                    if (parsed.ContainsKey("results") && parsed.ContainsKey("total"))
                    {
                        summary = parsed;
                        return true;
                    }
                }
                catch
                {
                }
            }
            return false;
        }

        public static List<Dictionary<string, object>> ScanResultRows(Dictionary<string, object> summary)
        {
            var results = new List<Dictionary<string, object>>();
            if (summary != null
                && summary.TryGetValue("results", out object rawResults)
                && rawResults is List<object> items)
            {
                foreach (object item in items)
                {
                    if (item is Dictionary<string, object> map)
                    {
                        results.Add(map);
                    }
                }
            }
            return results;
        }

        public static bool IsDirectProbeRow(Dictionary<string, object> row)
        {
            return BackendJson.TryGetMap(row, "probe", out _);
        }

        public static string FormatDirectProbeSummary(
            List<Dictionary<string, object>> results,
            Dictionary<string, object> summary)
        {
            results ??= new List<Dictionary<string, object>>();
            summary ??= new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            int directDeactivated = results.Count(ProbeDeactivated);
            int directOk = results.Count(row =>
                !ProbeDeactivated(row)
                && ProbeSucceeded(row));
            int direct401 = results.Count(row =>
                !ProbeDeactivated(row)
                && ProbeReturned401(row));
            int directFailed = Math.Max(0, results.Count - directOk - direct401 - directDeactivated);
            int reloginAttempted = int.TryParse(
                BackendJson.GetString(summary, "relogin_attempted"),
                NumberStyles.Integer,
                CultureInfo.InvariantCulture,
                out int parsedReloginAttempted)
                ? parsedReloginAttempted
                : 0;
            string directSummary = "总数：" + results.Count
                + "    AT有效：" + directOk
                + "    AT失效：" + direct401
                + "    账号停用：" + directDeactivated
                + "    其他失败：" + directFailed;
            if (reloginAttempted > 0)
            {
                directSummary += "    重登成功：" + BackendJson.GetString(summary, "relogin_success")
                    + "    重登失败：" + BackendJson.GetString(summary, "relogin_failed")
                    + "    确认停用：" + BackendJson.GetString(summary, "relogin_account_deactivated");
            }
            return directSummary;
        }

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

        public static bool ProbeSucceeded(Dictionary<string, object> row)
        {
            return BackendJson.TryGetMap(row, "probe", out Dictionary<string, object> probe) && BackendJson.GetBool(probe, "ok");
        }

        public static bool ProbeReturned401(Dictionary<string, object> row)
        {
            if (!BackendJson.TryGetMap(row, "probe", out Dictionary<string, object> probe)) return false;
            string status = BackendJson.GetString(probe, "status").Trim().ToLowerInvariant();
            return BackendJson.GetString(probe, "status_code") == "401" || status == "token_invalid";
        }

        public static bool ProbeDeactivated(Dictionary<string, object> row)
        {
            if (row == null) return false;
            if (MapDeactivated(row)) return true;
            if (BackendJson.TryGetMap(row, "probe", out Dictionary<string, object> probe)
                && MapDeactivated(probe))
            {
                return true;
            }
            return BackendJson.TryGetMap(row, "relogin", out Dictionary<string, object> relogin)
                && MapDeactivated(relogin);
        }

        public static bool MapDeactivated(Dictionary<string, object> data)
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
                {
                    return true;
                }
            }
            return false;
        }

        public static string ProbeStatusLabel(Dictionary<string, object> probe)
        {
            if (MapDeactivated(probe))
            {
                return "账号停用";
            }
            if (BackendJson.GetString(probe, "status_code") == "401" || BackendJson.GetString(probe, "status").Equals("token_invalid", StringComparison.OrdinalIgnoreCase))
            {
                return "AT失效 / HTTP 401";
            }
            if (BackendJson.GetBool(probe, "ok"))
            {
                string statusCode = BackendJson.GetString(probe, "status_code");
                return statusCode.Length > 0 ? "AT有效 / HTTP " + statusCode : "AT有效";
            }
            string failedCode = BackendJson.GetString(probe, "status_code");
            return failedCode.Length > 0 ? "测活失败 / HTTP " + failedCode : "测活失败";
        }

        public static string ScanResultError(Dictionary<string, object> row)
        {
            foreach (string section in new[] { "oauth", "refresh" })
            {
                if (BackendJson.TryGetMap(row, section, out Dictionary<string, object> map))
                {
                    string error = BackendJson.GetString(map, "error");
                    if (error.Length > 0) return error;
                }
            }
            return "";
        }

        /// <summary>
        /// Detail text for the single-account quota-usage probe shown by the
        /// context-menu liveness check.
        /// </summary>
        public static string FormatLivenessDetail(JsonElement root)
        {
            var sb = new StringBuilder();
            string statusCode = root.TryGetProperty("status_code", out JsonElement codeEl) ? codeEl.ToString() : "";
            sb.AppendLine("状态：AT 有效");
            sb.AppendLine("接口：HTTP " + (string.IsNullOrWhiteSpace(statusCode) ? "200" : statusCode));
            return sb.ToString().TrimEnd();
        }
    }
}
