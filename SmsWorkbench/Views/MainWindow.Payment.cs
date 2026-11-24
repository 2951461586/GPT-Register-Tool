using SmsWorkbench.Localization;

namespace SmsWorkbench.Views
{
    public partial class MainWindow
    {
        // Payment-link actions and unified protocol extractor.
        // CLI argument construction is delegated to BackendCommandPlanner and
        // PaymentBatchService; backend JSON interpretation is delegated to
        // BackendResultInterpreter.

        // OpenSessions_Click / OpenDatabase_Click / OpenMailboxPool_Click were
        // removed (2026-09-02, round 6): dead event handlers with no XAML
        // subscriber -- the menu items they served are gone, only the handlers
        // were left behind. OpenPath() itself is still used below.

        private void OpenPayPalLink()
        {
            PoolRow? row = SelectedEmailRowOrNotify("打开支付链接");
            if (row == null) return;
            if (string.IsNullOrWhiteSpace(row.PayPalUrl))
            {
                ShowThemedInfoDialog(Loc.T("无支付链接"), Loc.T("选中账号没有可打开的支付链接。"));
                return;
            }
            OpenPayPalUrl(row.PayPalUrl, row.Identifier);
        }

        // AtExtractBaLink_Click / ShowProtocolPaymentDialog removed
        // (2026-09-02, round 6): no XAML subscriber and no caller.
        // The single-account protocol payment window was retired on 2026-10-24:
        // it had no entry point, so the window, its view model/service, the
        // stage-matrix store and the dialog service were deleted. Batch protocol
        // payment (the only supported entry) stays below.

        private void BatchProtocolPayment()
        {
            var rows = SelectedRowsOrCurrent()
                .Where(row => row != null && !string.IsNullOrWhiteSpace(row.Identifier))
                .ToList();
            ShowPaymentBatchDialog(rows);
        }

        private void ShowPaymentBatchDialog(IEnumerable<PoolRow> rows)
        {
            // Only one backend task may run at a time — the coordinator enforces
            // this and would reject a second concurrent run with
            // BackendTaskAlreadyRunningException. Block at the UI instead of
            // letting the dialog open and the run immediately fail.
            if (backendTasks.IsRunning)
            {
                ShowThemedInfoDialog(
                    Loc.T("任务进行中"),
                    Loc.T("已有后端任务正在运行，请先等待其完成或取消后再发起支付链接提取。"));
                return;
            }

            var accounts = rows.Select(row => new PaymentBatchAccount(row.Identifier, row.HasAccessToken));
            if (paymentBatchDialogs.ShowDialog(this, accounts))
                RefreshPools();
        }
    }
}
