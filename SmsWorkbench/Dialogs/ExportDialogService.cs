namespace SmsWorkbench.Dialogs
{
    /// <summary>
    /// The dialogs the import/export actions open.
    /// </summary>
    public interface IExportDialogService
    {
        /// <summary>Format picker; empty string when the operator cancels.</summary>
        string ShowExportFormat(Window owner);

        /// <summary>"Export finished" summary with an "open folder" action.</summary>
        void ShowExportComplete(
            Window owner,
            string outputPath,
            int exportedCount,
            int skippedCount,
            string formatLabel,
            string formatDescription);

        /// <summary>
        /// Account-batch result panel. Returns <c>false</c> when the backend output
        /// carried no summary, so the caller can report that with its own dialog.
        /// </summary>
        bool ShowAccountScanResult(Window owner, string backendOutput, BackendAccountResultKind resultKind);

        /// <summary>CPA / SUB2API import target picker; empty string on cancel.</summary>
        string ShowImportTarget(Window owner, string title);
    }

    public sealed class ExportDialogService : IExportDialogService
    {
        private readonly Action<string> openPath;
        private readonly Func<string> runtimeDirectory;

        public ExportDialogService(Action<string> openPath, Func<string> runtimeDirectory)
        {
            this.openPath = openPath;
            this.runtimeDirectory = runtimeDirectory;
        }

        public string ShowExportFormat(Window owner)
        {
            var dialog = new ExportFormatDialog { Owner = owner };
            return dialog.ShowDialog() == true ? dialog.SelectedFormat : "";
        }

        public void ShowExportComplete(
            Window owner,
            string outputPath,
            int exportedCount,
            int skippedCount,
            string formatLabel,
            string formatDescription)
        {
            var dialog = new ExportCompleteDialog(
                owner,
                outputPath,
                exportedCount,
                skippedCount,
                formatLabel,
                formatDescription,
                openPath,
                runtimeDirectory);
            dialog.ShowDialog();
        }

        public bool ShowAccountScanResult(Window owner, string backendOutput, BackendAccountResultKind resultKind)
        {
            string title = BackendResultInterpreter.AccountResultTitle(resultKind);
            var summary = BackendResultInterpreter.TryExtractScanSummary(backendOutput);
            if (summary == null) return false;

            var results = new List<Dictionary<string, object>>();
            if (summary.TryGetValue("results", out object? rawResults) && rawResults is List<object> items)
            {
                foreach (object item in items)
                {
                    if (item is Dictionary<string, object> map)
                    {
                        results.Add(map);
                    }
                }
            }

            bool directProbe = results.Any(r => BackendJson.TryGetMap(r, "probe", out _));
            bool isPromotion = BackendResultInterpreter.IsPromotionRows(results);
            bool isQualification = !isPromotion && BackendResultInterpreter.IsPaymentQualificationRows(results);
            var rtRows = directProbe ? new List<Dictionary<string, object>>() : results.Where(r => BackendJson.GetBool(r, "has_rt")).ToList();
            var noRtRows = directProbe ? results : results.Where(r => !BackendJson.GetBool(r, "has_rt")).ToList();

            string summaryLine = isQualification
                ? BackendResultInterpreter.PaymentQualificationSummary(results)
                : isPromotion
                ? BackendResultInterpreter.PromotionSummary(results)
                : directProbe
                ? ExportViewModel.FormatDirectProbeSummary(results, summary)
                : "总数：" + BackendJson.GetString(summary, "total")
                    + "    正常：" + BackendJson.GetString(summary, "alive")
                    + "    掉号：" + BackendJson.GetString(summary, "account_deactivated")
                    + "    401/AT失效：" + BackendJson.GetString(summary, "at_invalid")
                    + "    手机验证：" + BackendJson.GetString(summary, "secondary_phone_verification_required")
                    + "    失败：" + BackendJson.GetString(summary, "failed");

            string noRtSectionTitle = isPromotion ? title + "结果" : (directProbe ? "AT 测活结果" : "未接码号结果");
            var dialog = new AccountScanResultDialog(owner, title, summaryLine, noRtRows, noRtSectionTitle, rtRows);
            dialog.ShowDialog();
            return true;
        }

        public string ShowImportTarget(Window owner, string title)
        {
            var dialog = new ImportTargetDialog(owner, title);
            return dialog.ShowDialog() == true ? dialog.SelectedTarget : "";
        }
    }
}
