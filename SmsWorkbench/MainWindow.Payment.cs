// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Payment-link actions and unified protocol extractor.
        // CLI argument construction is delegated to BackendCommandPlanner;
        // backend JSON interpretation is delegated to ProtocolPaymentResultPresenter
        // and BackendResultInterpreter.

        // OpenSessions_Click / OpenDatabase_Click / OpenMailboxPool_Click were
        // removed (2026-09-02, round 6): dead event handlers with no XAML
        // subscriber -- the menu items they served are gone, only the handlers
        // were left behind. OpenPath() itself is still used below.

        private void OpenPayPalLink_Click(object sender, RoutedEventArgs e)
        {
            PoolRow? row = SelectedEmailRowOrNotify("打开支付链接");
            if (row == null) return;
            if (string.IsNullOrWhiteSpace(row.PayPalUrl))
            {
                MessageBox.Show("选中账号没有可打开的支付链接。", "无支付链接", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            OpenPayPalUrl(row.PayPalUrl, row.Identifier);
        }

        // AtExtractBaLink_Click / ShowProtocolPaymentDialog removed
        // (2026-09-02, round 6): no XAML subscriber and no caller. The protocol
        // payment window is opened through protocolPaymentDialogs from
        // elsewhere; this wrapper duplicated that path with its own guard.

    }
}
