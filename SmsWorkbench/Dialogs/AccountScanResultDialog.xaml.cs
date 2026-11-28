using System.Windows.Media;

using SmsWorkbench.Localization;

namespace SmsWorkbench.Dialogs
{
    public partial class AccountScanResultDialog : Window
    {
        public AccountScanResultDialog(
            Window owner,
            string title,
            string summaryLine,
            List<Dictionary<string, object>> noRtRows,
            string noRtSectionTitle,
            List<Dictionary<string, object>> rtRows)
        {
            InitializeComponent();
            WindowThemeService.Attach(this);
            Owner = owner;
            Title = Loc.F("{0}结果", title);
            HeaderTitleTextBlock.Text = Loc.F("{0}完成", title);
            SummaryTextBlock.Text = summaryLine;

            if (noRtRows.Count > 0)
            {
                AddSection(noRtSectionTitle, noRtRows);
            }
            if (rtRows.Count > 0)
            {
                AddSection("已接码号结果", rtRows);
            }
            if (BodyStackPanel.Children.Count == 0)
            {
                var emptyText = new TextBlock
                {
                    Text = Loc.F("没有可展示的{0}明细。", title),
                };
                emptyText.SetResourceReference(TextBlock.ForegroundProperty, "TextSub");
                BodyStackPanel.Children.Add(emptyText);
            }
        }

        private void AddSection(string sectionTitle, List<Dictionary<string, object>> rows)
        {
            var header = new TextBlock
            {
                Text = sectionTitle + "（" + rows.Count + "）",
                FontWeight = FontWeights.SemiBold,
                Margin = new Thickness(0, BodyStackPanel.Children.Count == 0 ? 0 : 12, 0, 8),
            };
            header.SetResourceReference(TextBlock.FontSizeProperty, "Type.Subtitle.Size");
            header.SetResourceReference(TextBlock.ForegroundProperty, "TextMain");
            BodyStackPanel.Children.Add(header);

            var card = new Border
            {
                BorderThickness = new Thickness(1),
                Padding = new Thickness(10),
                Margin = new Thickness(0, 0, 0, 4),
            };
            card.SetResourceReference(Border.BackgroundProperty, "Surface.Card");
            card.SetResourceReference(Border.BorderBrushProperty, "Divider");
            card.SetResourceReference(Border.CornerRadiusProperty, "Corner.Lg");
            var stack = new StackPanel();
            foreach (Dictionary<string, object> row in rows)
            {
                string email = BackendJson.GetString(row, "email");
                string status;
                string error;
                if (BackendJson.TryGetMap(row, "probe", out var probe))
                {
                    status = BackendResultInterpreter.ResultRowStatus(row);
                    if (BackendJson.TryGetMap(row, "relogin", out var relogin)
                        && !BackendJson.GetBool(relogin, "ok"))
                    {
                        error = BackendJson.GetString(relogin, "error");
                    }
                    else
                    {
                        error = BackendJson.GetString(probe, "error");
                    }
                }
                else if (BackendJson.GetString(row, "error_code") == "account_not_found")
                {
                    status = "支付资格未知";
                    error = "本地账号不存在";
                }
                else
                {
                    status = BackendResultInterpreter.ScanStatusLabel(BackendJson.GetString(row, "scan_status"));
                    error = BackendResultInterpreter.ScanResultError(row);
                }

                string line = error.Length > 0 ? email + "  ·  " + status + "  ·  " + error : email + "  ·  " + status;
                var rowText = new TextBlock
                {
                    Text = line,
                    TextWrapping = TextWrapping.Wrap,
                    LineHeight = 20,
                    Margin = new Thickness(0, 0, 0, 6),
                };
                rowText.SetResourceReference(TextBlock.ForegroundProperty, "TextSub");
                stack.Children.Add(rowText);
            }
            card.Child = stack;
            BodyStackPanel.Children.Add(card);
        }

        private void Close_Click(object sender, RoutedEventArgs e) => Close();
    }
}
