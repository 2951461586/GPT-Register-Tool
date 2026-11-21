using SmsWorkbench.Localization;

namespace SmsWorkbench.Dialogs
{
    public partial class MailDetailDialogWindow : Window
    {
        private readonly string content;
        private readonly string code;
        private readonly Action<string> log;

        public MailDetailDialogWindow(
            Window owner,
            MailItem item,
            string content,
            string code,
            Action<string> log)
        {
            this.content = content;
            this.code = code;
            this.log = log;

            InitializeComponent();
            Owner = owner;
            Title = item.Subject.Length > 0 ? item.Subject : Loc.T("邮件详情");
            SubjectTextBlock.Text = item.Subject;
            MetaTextBlock.Text = item.ReceivedAt + "    " + item.From;
            BodyTextBox.Text = content;
            CopyCodeButton.Content = code.Length > 0 ? Loc.F("复制验证码 {0}", code) : Loc.T("未识别验证码");
            CopyCodeButton.IsEnabled = code.Length > 0;
        }

        private void CopyCodeButton_Click(object sender, RoutedEventArgs e)
        {
            if (code.Length == 0) return;
            Clipboard.SetText(code);
            log(Loc.T("验证码已复制：") + code);
        }

        private void CopyBodyButton_Click(object sender, RoutedEventArgs e)
        {
            Clipboard.SetText(content);
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e) => Close();
    }
}
