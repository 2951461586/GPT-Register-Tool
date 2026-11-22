using SmsWorkbench.Localization;

namespace SmsWorkbench.Dialogs
{
    /// <summary>
    /// Account detail panel. XAML since 2026-11-20: the window partial used to
    /// build the same Grid/StackPanel/Border tree in ~250 lines of code.
    /// </summary>
    /// <remarks>
    /// The dialog owns only presentation and the one self-contained side effect it
    /// always owned (copying the access token, with a 1.2 s "已复制" label flip).
    /// Every window action arrives through <see cref="IAccountDetailActions"/>, so
    /// the panel is constructible in <c>dotnet test</c> and the buttons cannot
    /// quietly start doing window work.
    /// </remarks>
    public partial class AccountDetailDialog : Window
    {
        private readonly PoolRow row;
        private readonly AccountDetailView view;
        private readonly IAccountDetailActions actions;

        public AccountDetailDialog(Window owner, PoolRow row, AccountDetailView view, IAccountDetailActions actions)
        {
            this.row = row;
            this.view = view;
            this.actions = actions;

            InitializeComponent();
            WindowThemeService.Attach(this);
            Owner = owner;
            DataContext = view;
            Title = Loc.F("账号详情 - {0}", view.Identifier);
        }

        private void OpenSourceFile_Click(object sender, RoutedEventArgs e) => actions.OpenSourceFile(row);

        private void OpenPayPal_Click(object sender, RoutedEventArgs e) => actions.OpenPayPal(row);

        private void CopyPayPal_Click(object sender, RoutedEventArgs e) => actions.CopyPayPal(row);

        private void CheckAlive_Click(object sender, RoutedEventArgs e)
        {
            // Close first: the probe opens its own progress UI and the original
            // behaviour was "the panel is gone before the probe starts".
            Close();
            actions.CheckAlive(row);
        }

        private void Close_Click(object sender, RoutedEventArgs e) => Close();

        private void CopyAccessToken_Click(object sender, RoutedEventArgs e) => _ = CopyAccessTokenAsync();

        private async Task CopyAccessTokenAsync()
        {
            if (!view.HasAccessToken) return;
            try
            {
                Clipboard.SetText(view.AccessToken);
                CopyAccessTokenButton.Content = Loc.T("已复制");
                await Task.Delay(1200);
                CopyAccessTokenButton.Content = Loc.T("一键复制AT");
            }
            catch (Exception error)
            {
                // An event handler must not throw into the dispatcher; a clipboard
                // owned by another process is the realistic failure here.
                CopyAccessTokenButton.Content = Loc.T("一键复制AT");
                Log.Debug(error, "copy access token failed");
            }
        }
    }
}
