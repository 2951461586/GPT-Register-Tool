using System.Windows.Input;

using SmsWorkbench.Localization;

namespace SmsWorkbench.Dialogs
{
    public partial class InfoConfirmDialogWindow : Window
    {
        public bool Confirmed { get; private set; }

        public InfoConfirmDialogWindow(
            Window owner,
            string title,
            string message,
            string? confirmText = null,
            bool showCancel = false,
            bool isDanger = false)
        {
            InitializeComponent();
            WindowThemeService.Attach(this);
            Owner = owner;
            Title = title;
            TitleTextBlock.Text = title;
            MessageTextBlock.Text = message;
            PreviewKeyDown += OnPreviewKeyDown;

            if (showCancel)
            {
                Width = 460;
                MinWidth = 420;
                CancelButton.Visibility = Visibility.Visible;
                CancelButton.Content = Loc.T("取消");
                ConfirmButton.Content = confirmText ?? Loc.T("确认");
                ConfirmButton.Width = 76;
                ConfirmButton.MinWidth = 76;
                if (isDanger)
                {
                    ConfirmButton.Style = (Style)FindResource("DangerButton");
                }
            }
            else
            {
                ConfirmButton.Content = confirmText ?? Loc.T("知道了");
            }
        }

        private void OnPreviewKeyDown(object sender, KeyEventArgs e)
        {
            if (e.Key == Key.Escape && CancelButton.Visibility != Visibility.Visible)
            {
                e.Handled = true;
                Confirmed = false;
                Close();
            }
        }

        private void CancelButton_Click(object sender, RoutedEventArgs e)
        {
            Confirmed = false;
            Close();
        }

        private void ConfirmButton_Click(object sender, RoutedEventArgs e)
        {
            Confirmed = true;
            Close();
        }
    }
}
