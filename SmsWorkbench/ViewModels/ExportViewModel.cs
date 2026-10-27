// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// Which rows an export or import acts on, and the wording shown for each
    /// export format. Window-independent so the selection fallbacks and labels
    /// are xunit-testable (placement rule 8); dialogs, file writes and backend
    /// calls stay in MainWindow.Export.cs.
    /// </summary>
    public sealed class ExportViewModel
    {
        private readonly PoolViewModel pool;

        public ExportViewModel(PoolViewModel pool)
        {
            this.pool = pool;
        }

        /// <summary>
        /// Checked/selected rows, else everything matching the current filter,
        /// else the whole pool.
        /// </summary>
        public List<PoolRow> CandidateRows(List<PoolRow> selected)
        {
            List<PoolRow> rows = selected;
            if (rows.Count == 0) rows = pool.FilteredRows();
            if (rows.Count == 0) rows = pool.AllRows.ToList();
            return rows;
        }

        /// <summary>
        /// Importable rows from the selection (one per email), falling back to
        /// every importable row in the pool when the selection has none.
        /// </summary>
        public List<PoolRow> ImportableRows(IEnumerable<PoolRow> selected)
        {
            List<PoolRow> fromSelection = Distinct(selected);
            return fromSelection.Count > 0 ? fromSelection : Distinct(pool.AllRows);

            static List<PoolRow> Distinct(IEnumerable<PoolRow> rows) => rows
                .Where(IsImportable)
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
        }

        public static bool IsImportable(PoolRow? row)
        {
            if (row == null) return false;
            if (string.IsNullOrWhiteSpace(row.Identifier)) return false;
            if (row.HasAccessToken) return true;
            // The status/payment labels are localized display copy, so the
            // decision goes through the language-agnostic matcher instead of a
            // hand-maintained pair of literal lists. The hand-written English
            // half that used to live here is what the matcher now derives from
            // the same catalog, so the two can no longer drift.
            string status = (row.Status + " " + row.PayPalStatus).Trim();
            return Loc.DisplayContains(status, "已注册")
                || Loc.DisplayContains(status, "待支付")
                || Loc.DisplayContains(status, "支付完成")
                || Loc.DisplayContains(status, "PM已创建")
                || Loc.DisplayContains(status, "已导入");
        }

        public static string FormatLabel(string format)
        {
            string value = (format ?? "").Trim().ToLowerInvariant();
            if (value == "sub2api") return "SUB2API JSON";
            if (value == "cockpit") return "Cockpit JSON";
            if (value == "9router") return "9router JSON";
            if (value == "codex") return "Codex auth.json";
            if (value == "axonhub") return "AxonHub JSON";
            if (value == "codexmanager") return "Codex-Manager JSON";
            if (value == "json") return Loc.T("原始 JSON");
            if (value == "txt") return "TXT";
            return "CPA JSON";
        }

        public static string FormatDescription(string format)
        {
            string value = (format ?? "").Trim().ToLowerInvariant();
            if (value == "sub2api") return Loc.T("由 session_converter.py 生成的 Sub2API accounts 文档");
            if (value == "cockpit") return Loc.T("由 session_converter.py 生成的 Cockpit/Codex 导入结构");
            if (value == "9router") return Loc.T("由 session_converter.py 生成的 9router provider 结构");
            if (value == "codex") return Loc.T("由 session_converter.py 生成的 Codex auth.json 结构");
            if (value == "axonhub") return Loc.T("由 session_converter.py 生成的 AxonHub 结构；缺少 RT 时会写入占位提示");
            if (value == "codexmanager") return Loc.T("由 session_converter.py 生成的 Codex-Manager 结构");
            return Loc.T("由 session_converter.py 生成的 CPA JSON；缺少 id_token 时会合成兼容字段");
        }

        public static string FormatDirectProbeSummary(
            List<Dictionary<string, object>> results,
            Dictionary<string, object> summary)
        {
            results ??= new List<Dictionary<string, object>>();
            summary ??= new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            int directDeactivated = results.Count(BackendResultInterpreter.IsProbeDeactivated);
            int directOk = results.Count(row =>
                !BackendResultInterpreter.IsProbeDeactivated(row)
                && BackendResultInterpreter.IsProbeSucceeded(row));
            int direct401 = results.Count(row =>
                !BackendResultInterpreter.IsProbeDeactivated(row)
                && BackendResultInterpreter.IsProbeReturned401(row));
            int directFailed = Math.Max(0, results.Count - directOk - direct401 - directDeactivated);
            _ = int.TryParse(
                BackendJson.GetString(summary, "relogin_attempted"),
                NumberStyles.Integer,
                CultureInfo.InvariantCulture,
                out int reloginAttempted);
            string directSummary = Loc.T("总数：") + results.Count
                + "    " + Loc.T("AT有效：") + directOk
                + "    " + Loc.T("AT失效：") + direct401
                + "    " + Loc.T("账号停用：") + directDeactivated
                + "    " + Loc.T("其他失败：") + directFailed;
            if (reloginAttempted > 0)
            {
                directSummary += "    " + Loc.T("重登成功：") + BackendJson.GetString(summary, "relogin_success")
                    + "    " + Loc.T("重登失败：") + BackendJson.GetString(summary, "relogin_failed")
                    + "    " + Loc.T("确认停用：") + BackendJson.GetString(summary, "relogin_account_deactivated");
            }
            return directSummary;
        }
    }
}
