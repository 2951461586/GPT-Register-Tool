namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Payment-link actions and unified protocol extractor.
        // CLI argument construction is delegated to BackendCommandPlanner;
        // backend JSON interpretation is delegated to ProtocolPaymentResultPresenter
        // and BackendResultInterpreter.

        private void OpenSessions_Click(object sender, RoutedEventArgs e) => OpenPath(GetSessionsDir());

        private void OpenDatabase_Click(object sender, RoutedEventArgs e) => OpenPath(GetDatabasePath());

        private void OpenMailboxPool_Click(object sender, RoutedEventArgs e) => OpenPath(GetMailboxTokenFile());

        private void OpenPayPalLink_Click(object sender, RoutedEventArgs e)
        {
            PoolRow row = SelectedEmailRowOrNotify("打开支付链接");
            if (row == null) return;
            if (string.IsNullOrWhiteSpace(row.PayPalUrl))
            {
                MessageBox.Show("选中账号没有可打开的支付链接。", "无支付链接", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            OpenPayPalUrl(row.PayPalUrl, row.Identifier);
        }

        private void AtExtractBaLink_Click(object sender, RoutedEventArgs e)
        {
            var selected = SelectedRowsOrCurrent()
                .Where(row => !string.IsNullOrWhiteSpace(row.Identifier))
                .GroupBy(row => row.Identifier.Trim().ToLowerInvariant())
                .Select(group => group.First())
                .ToList();
            ShowPaymentBatchDialog(selected);
        }

        /// <summary>
        /// Unified protocol payment-link extractor.
        /// Uses ProtocolPaymentExecutionPlanner for CLI construction and
        /// ProtocolPaymentResultPresenter for JSON interpretation.
        /// Error handling is unified via BackendResultInterpreter.
        /// </summary>
        private void ShowProtocolPaymentDialog(PoolRow selectedAccount = null)
        {
            // Same single-backend-task guard as the batch dialog: the coordinator
            // rejects a concurrent run, so surface it here instead of failing
            // once the dialog's run begins.
            if (backendTasks.IsRunning)
            {
                MessageBox.Show(
                    this,
                    "已有后端任务正在运行，请先等待其完成或取消后再发起协议支付。",
                    "任务进行中",
                    MessageBoxButton.OK,
                    MessageBoxImage.Information);
                return;
            }

            ProtocolPaymentAccount account = selectedAccount == null
                ? null
                : new ProtocolPaymentAccount(selectedAccount.Identifier, SessionFileFor(selectedAccount));
            if (protocolPaymentDialogs == null)
            {
                MessageBox.Show(this, "协议支付对话框服务未配置。", "配置错误", MessageBoxButton.OK, MessageBoxImage.Warning);
                return;
            }
            protocolPaymentDialogs.ShowDialog(this, account);
        }

    }
}
