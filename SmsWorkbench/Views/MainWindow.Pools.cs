namespace SmsWorkbench.Views
{
    public partial class MainWindow
    {
        private readonly PoolRefreshCoordinator poolRefresh;

        private bool IsMailboxPoolLikeRow(PoolRow row)
        {
            if (row == null) return false;
            return MailboxPoolFileStore.IsMailboxPoolLike(row.AccountType, row.MailboxProvider);
        }

        private void RefreshPools()
        {
            RunUiTask(() => RefreshPoolsAsync(ct: _lifetimeCts.Token));
        }

        private void RefreshPoolsPreservingView(bool forceFull = false)
        {
            RunUiTask(() => RefreshPoolsAsync(preserveView: true, forceFull: forceFull, ct: _lifetimeCts.Token));
        }

        private Task RefreshPoolsAsync(bool preserveView = false, bool forceFull = false, CancellationToken ct = default)
        {
            return poolRefresh.RefreshPoolsAsync(
                desktopRead,
                Pool,
                StatusBar,
                GetChataiMailboxFilePath(),
                GetDatabasePath,
                JsonElementToDictionary,
                Mask,
                Log,
                preserveView,
                forceFull,
                System.ComponentModel.DesignerProperties.GetIsInDesignMode(this),
                ct);
        }

        private string NormalizeEmailKey(string email)
        {
            return MailboxPoolFileStore.NormalizeEmailKey(email);
        }

        private string GetChataiMailboxFilePath()
        {
            return PoolRefreshCoordinator.ResolveChataiMailboxFilePath(
                Register.ChataiMailboxFilePath,
                configDir,
                rootDir);
        }
    }
}
