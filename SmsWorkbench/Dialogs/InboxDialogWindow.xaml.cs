using System.Windows.Input;

using SmsWorkbench.Localization;

namespace SmsWorkbench.Dialogs
{
    public partial class InboxDialogWindow : Window
    {
        private readonly string identifier;
        private readonly Action refreshAction;
        private readonly Action<string> log;
        private string activeBody = "";
        private string activeCode = "";

        public ObservableCollection<MailItem> MailItems { get; } = new();

        public bool IsShowingDetail => DetailPanel.Visibility == Visibility.Visible;

        public InboxDialogWindow(
            Window owner,
            string identifier,
            Action refreshAction,
            Action<string> log)
        {
            this.identifier = identifier;
            this.refreshAction = refreshAction;
            this.log = log;

            InitializeComponent();
            WindowThemeService.Attach(this);
            Owner = owner;
            Title = Loc.F("收件箱 - {0}", identifier);
            MailDataGrid.ItemsSource = MailItems;
            PreviewKeyDown += InboxDialogWindow_PreviewKeyDown;
            MouseDown += InboxDialogWindow_MouseDown;
        }

        public void SetHeaderText(string text)
        {
            HeaderTextBlock.Text = text;
        }

        public void ShowDetail(MailItem item)
        {
            if (item == null) return;
            string content = MailBodyFormatter.ToDisplayText(item.Body, item.BodyPreview);
            if (content.Length == 0) content = Loc.T("（邮件正文为空）");
            string code = item.VerificationCode.Length > 0
                ? item.VerificationCode
                : InboxReader.ExtractCode(content);

            activeBody = content;
            activeCode = code;

            string subject = item.Subject.Length > 0 ? item.Subject : Loc.T("邮件详情");
            Title = Loc.F("收件箱 - {0}", identifier) + " · " + subject;
            DetailSubjectTextBlock.Text = subject;
            DetailMetaTextBlock.Text = item.ReceivedAt + "    " + item.From;
            DetailBodyTextBox.Text = content;
            DetailBodyTextBox.ScrollToHome();
            CopyCodeButton.Content = code.Length > 0 ? Loc.F("复制验证码 {0}", code) : Loc.T("未识别验证码");
            CopyCodeButton.IsEnabled = code.Length > 0;
            CopyBodyButton.Content = Loc.T("复制正文");

            ListPanel.Visibility = Visibility.Collapsed;
            DetailPanel.Visibility = Visibility.Visible;
            BackToListButton.Focus();
        }

        public void ShowList()
        {
            DetailPanel.Visibility = Visibility.Collapsed;
            ListPanel.Visibility = Visibility.Visible;
            Title = Loc.F("收件箱 - {0}", identifier);
            MailDataGrid.Focus();
        }

        private void RefreshButton_Click(object sender, RoutedEventArgs e)
        {
            refreshAction();
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e) => Close();

        private void OpenDetailButton_Click(object sender, RoutedEventArgs e)
        {
            if (MailDataGrid.SelectedItem is MailItem item)
            {
                ShowDetail(item);
            }
        }

        private void BackToListButton_Click(object sender, RoutedEventArgs e) => ShowList();

        private void MailDataGrid_MouseDoubleClick(object sender, MouseButtonEventArgs e)
        {
            if (MailDataGrid.SelectedItem is MailItem item)
            {
                ShowDetail(item);
            }
        }

        private void MailDataGrid_SelectionChanged(object sender, SelectionChangedEventArgs e)
        {
            OpenDetailButton.IsEnabled = MailDataGrid.SelectedItem is MailItem;
        }

        private void MailDataGrid_PreviewKeyDown(object sender, KeyEventArgs e)
        {
            if (e.Key == Key.Enter && MailDataGrid.SelectedItem is MailItem item)
            {
                e.Handled = true;
                ShowDetail(item);
            }
        }

        private void InboxDialogWindow_PreviewKeyDown(object sender, KeyEventArgs e)
        {
            if (IsShowingDetail)
            {
                if (e.Key == Key.Escape || (e.Key == Key.Left && Keyboard.Modifiers == ModifierKeys.Alt))
                {
                    e.Handled = true;
                    ShowList();
                }
                return;
            }

            if (e.Key == Key.Escape)
            {
                e.Handled = true;
                Close();
            }
        }

        private void InboxDialogWindow_MouseDown(object sender, MouseButtonEventArgs e)
        {
            if (e.ChangedButton == MouseButton.XButton1 && IsShowingDetail)
            {
                e.Handled = true;
                ShowList();
            }
        }

        private void CopyCodeButton_Click(object sender, RoutedEventArgs e) => _ = CopyCodeAsync();

        private async Task CopyCodeAsync()
        {
            if (activeCode.Length == 0) return;
            string copiedCode = activeCode;
            try
            {
                Clipboard.SetText(copiedCode);
                log(Loc.T("验证码已复制：") + copiedCode);
                CopyCodeButton.Content = Loc.T("已复制");
                await Task.Delay(1200);
                if (activeCode == copiedCode)
                {
                    CopyCodeButton.Content = Loc.F("复制验证码 {0}", copiedCode);
                }
            }
            catch (Exception error)
            {
                Log.Debug(error, "copy verification code failed");
            }
        }

        private void CopyBodyButton_Click(object sender, RoutedEventArgs e) => _ = CopyBodyAsync();

        private async Task CopyBodyAsync()
        {
            try
            {
                Clipboard.SetText(activeBody);
                CopyBodyButton.Content = Loc.T("已复制");
                await Task.Delay(1200);
                CopyBodyButton.Content = Loc.T("复制正文");
            }
            catch (Exception error)
            {
                CopyBodyButton.Content = Loc.T("复制正文");
                Log.Debug(error, "copy mail body failed");
            }
        }
    }
}
