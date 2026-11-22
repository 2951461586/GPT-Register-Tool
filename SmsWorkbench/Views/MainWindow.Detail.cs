using SmsWorkbench.Localization;

namespace SmsWorkbench.Views
{
    public partial class MainWindow
    {
        // Account detail: assembly stays here, rendering lives in
        // Dialogs/AccountDetailDialog.xaml (moved 2026-11-20). Until then this
        // partial carried ~250 lines of Grid/StackPanel/Border construction, which
        // is why the panel had no test: nothing could be asserted without a window.
        private readonly AccountDetailAssembler accountDetail;

        private void ShowAccountDetail(PoolRow row)
            => RunUiTask(() => ShowAccountDetailAsync(row, _lifetimeCts.Token));

        private async Task ShowAccountDetailAsync(PoolRow row, CancellationToken ct = default)
        {
            if (row == null) return;
            AccountDetailView view = await BuildDetailViewAsync(row, ct);
            var dialog = new AccountDetailDialog(this, row, view, detailActions);
            dialog.ShowDialog();
        }

        private IAccountDetailActions detailActions => new DetailDialogActions(this);

        /// <summary>
        /// Everything the detail dialog shows, in the order the panel renders it.
        /// </summary>
        /// <remarks>
        /// The two "identity provenance" rows are conditional on purpose: an account
        /// registered without a proxy has no affinity, so the egress row is omitted
        /// rather than shown as an em dash — that is a real state, not missing data.
        /// </remarks>
        private async Task<AccountDetailView> BuildDetailViewAsync(PoolRow row, CancellationToken ct)
        {
            string accessToken = await accountDetail.ResolveAccessTokenAsync(row, ct);
            var items = new List<AccountDetailSummaryItem>
            {
                new(Loc.T("邮箱"), row.Identifier),
                new(Loc.T("类型"), row.AccountType ?? ""),
                new(Loc.T("状态"), row.Status ?? ""),
                new("Refresh Token", row.RefreshTokenStatus ?? ""),
                new(Loc.T("创建时间"), row.CreatedAt ?? ""),
                new(Loc.T("更新时间"), row.CompletedAt ?? ""),
            };

            return new AccountDetailView
            {
                Identifier = row.Identifier,
                Status = row.Status ?? "",
                SummaryItems = items
                    .Select(item => new AccountDetailSummaryItem(
                        item.Label,
                        string.IsNullOrWhiteSpace(item.Value) ? "—" : item.Value))
                    .ToArray(),
                EgressEndpoint = row.IdentityBound ? (row.EgressEndpoint ?? "").Trim() : "",
                EgressCountry = row.IdentityBound ? (row.EgressCountry ?? "").Trim() : "",
                FingerprintProfile = (row.FingerprintProfile ?? "").Trim(),
                PayPalUrl = row.PayPalUrl ?? "",
                AccessToken = accessToken,
            };
        }

        /// <summary>
        /// Binds the dialog's buttons to the commands this window already owns.
        /// </summary>
        /// <remarks>
        /// A nested adapter instead of five lambdas in the builder: the dialog gets a
        /// named object it cannot use for window work beyond these four commands, and
        /// the partial keeps its methods where the rest of the file's readers expect
        /// them.
        /// </remarks>
        private sealed class DetailDialogActions(MainWindow window) : IAccountDetailActions
        {
            public void OpenSourceFile(PoolRow row) => window.OpenAccountJson(row);

            public void OpenPayPal(PoolRow row) => window.OpenPayPalUrl(row.PayPalUrl ?? "", row.Identifier);

            public void CopyPayPal(PoolRow row) => window.CopyPayPalUrl(row.PayPalUrl ?? "", row.Identifier);

            public void CheckAlive(PoolRow row) => window.RunUiTask(() => window.CheckAccountAliveAsync(row));
        }

        private void OpenAccountJson(PoolRow row)
            => RunUiTask(() => OpenAccountJsonAsync(row, _lifetimeCts.Token));

        private async Task OpenAccountJsonAsync(PoolRow row, CancellationToken ct = default)
        {
            string path = await accountDetail.ResolveJsonPathAsync(row, ct);
            if (string.IsNullOrWhiteSpace(path))
            {
                ShowThemedInfoDialog(Loc.T("打开源文件"), Loc.T("未找到该账号对应的 JSON 文件。"));
                return;
            }
            OpenPath(path);
        }

        // AddDetailRow removed (2026-09-02, round 6): no caller. The account
        // detail panel is built from XAML bindings now; this was the last
        // code-behind row builder.
    }
}
