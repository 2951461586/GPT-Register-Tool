namespace SmsWorkbench
{
    public partial class MainWindow
    {
        private void BatchProtocolPayment_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedRowsOrCurrent()
                .Where(row => row != null && !string.IsNullOrWhiteSpace(row.Identifier))
                .ToList();
            ShowPaymentBatchDialog(rows);
        }

        private void ShowPaymentBatchDialog(IEnumerable<PoolRow> rows)
        {
            var accounts = rows.Select(row => new PaymentBatchAccount(row.Identifier, row.HasAccessToken));
            if (paymentBatchDialogs.ShowDialog(this, accounts))
                RefreshPools();
        }
    }
}
