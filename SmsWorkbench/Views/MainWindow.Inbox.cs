// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Inbox view and mail detail dialog
        private void ShowInboxDialog(PoolRow row)
            => RunUiTask(() => ShowInboxDialogAsync(row, _lifetimeCts.Token));

        private async Task ShowInboxDialogAsync(PoolRow row, CancellationToken ct = default)
        {
            var dialog = new Window
            {
                Title = Loc.F("收件箱 - {0}", row.Identifier),
                Owner = this,
                Width = 860,
                Height = 640,
                MinWidth = 700,
                MinHeight = 500,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(10) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var headerPanel = new StackPanel
            {
                Margin = new Thickness(0, 0, 0, 8)
            };
            var header = new TextBlock
            {
                Text = Loc.T("正在加载收件箱..."),
                FontSize = (double)FindResource("Type.Subtitle.Size"),
                FontWeight = FontWeights.SemiBold,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
            };
            headerPanel.Children.Add(header);
            Grid.SetRow(headerPanel, 0);
            root.Children.Add(headerPanel);

            var mailGrid = new DataGrid
            {
                AutoGenerateColumns = false,
                CanUserAddRows = false,
                HeadersVisibility = DataGridHeadersVisibility.Column,
                IsReadOnly = true,
                RowHeight = 28,
                GridLinesVisibility = DataGridGridLinesVisibility.Horizontal,
                AlternatingRowBackground = (System.Windows.Media.Brush)FindResource("GridAltBg"),
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderThickness = new Thickness(0)
            };
            mailGrid.Columns.Add(new DataGridTextColumn { Header = Loc.T("时间"), Binding = new System.Windows.Data.Binding("ReceivedAt"), Width = 150 });
            mailGrid.Columns.Add(new DataGridTextColumn { Header = Loc.T("发件人"), Binding = new System.Windows.Data.Binding("From"), Width = 200 });
            mailGrid.Columns.Add(new DataGridTextColumn { Header = Loc.T("主题"), Binding = new System.Windows.Data.Binding("Subject"), Width = new DataGridLength(1, DataGridLengthUnitType.Star) });
            Grid.SetRow(mailGrid, 1);
            root.Children.Add(mailGrid);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 8, 0, 0)
            };
            var refreshBtn = new Button { Content = Loc.T("刷新"), Width = 72 };
            var closeBtn = new Button { Content = Loc.T("关闭"), Width = 72 };
            actions.Children.Add(refreshBtn);
            actions.Children.Add(closeBtn);
            Grid.SetRow(actions, 2);
            root.Children.Add(actions);

            var mailItems = new ObservableCollection<MailItem>();
            mailGrid.ItemsSource = mailItems;

            closeBtn.Click += (_, __) => dialog.Close();

            async Task LoadEmails(CancellationToken ct = default)
            {
                if (IsCfWorkerRow(row) || IsReMailRow(row) || IsSmailrRow(row))
                {
                    header.Text = IsReMailRow(row) ? Loc.T("正在获取 ReMail 邮件...") : IsSmailrRow(row) ? Loc.T("正在获取 Smailr 邮件...") : Loc.T("正在获取 CFWorker 邮件...");
                    try
                    {
                        mailItems.Clear();
                        foreach (MailItem item in await FetchBackendInbox(row, 25, ct))
                        {
                            mailItems.Add(item);
                        }
                        header.Text = Loc.F("{0} - 最近 {1} 封邮件", row.Identifier, mailItems.Count);
                    }
                    catch (Exception ex)
                    {
                        header.Text = Loc.T("获取邮件失败：") + ex.Message;
                        Log((IsReMailRow(row) ? "ReMail" : IsSmailrRow(row) ? "Smailr" : "CFWorker") + Loc.T("收件箱获取失败：") + ex.Message);
                    }
                    return;
                }

                header.Text = Loc.T("正在刷新令牌...");
                try
                {
                    mailItems.Clear();
                    foreach (MailItem item in await FetchBackendInbox(row, 20, ct))
                    {
                        mailItems.Add(item);
                    }
                    header.Text = row.Identifier + " - " + mailItems.Count + " messages";
                }
                catch (Exception ex)
                {
                    header.Text = Loc.T("加载失败：") + ex.Message;
                    Log(Loc.T("收件箱加载异常：") + ex.Message);
                }
            }

            refreshBtn.Click += (_, __) => RunUiTask(() => LoadEmails(ct));
            mailGrid.MouseDoubleClick += (_, __) =>
            {
                if (mailGrid.SelectedItem is MailItem item)
                {
                    ShowMailDetailDialog(item);
                }
            };

            dialog.Content = root;
            dialog.Show();
            await LoadEmails(ct);
        }

        private async Task<List<MailItem>> FetchBackendInbox(PoolRow row, int limit, CancellationToken ct = default)
        {
            string remailToken = IsReMailRow(row) ? (row.MailboxToken ?? "").Trim() : "";
            string mailboxLine = await FindMailboxLineForRowAsync(row, ct).ConfigureAwait(true);
            if (mailboxLine.Length == 0 && BackendCommandPlanner.MailboxArgumentForLine(row.RawLine).Length > 0)
            {
                mailboxLine = row.RawLine;
            }
            // The planner owns the flag vocabulary, the temp mailbox file, the
            // session file, the proxy route, the ReMail environment and the
            // timeout; this partial only supplies the resolved values.
            BackendCommandPlan plan = BackendCommandPlanner.CreateViewInbox(
                row.Identifier,
                limit,
                BackendCommandPlanner.MailboxArgumentForLine(mailboxLine),
                mailboxLine,
                SessionFileFor(row),
                GetMailboxProxy(),
                remailToken);
            try
            {
                BackendCommandResult result = await backendClient.RunAsync(
                    BackendCommand.Create(
                        plan.TaskName,
                        plan.Arguments,
                        plan.TimeoutMilliseconds ?? 120000,
                        plan.Environment),
                    cancellationToken: ct);
                if (!result.Payload.HasValue)
                    throw new InvalidOperationException(BackendFailureMessage(result));
                JsonElement payload = result.Payload.Value;
                if (!payload.TryGetProperty("ok", out JsonElement ok) || !ok.GetBoolean())
                {
                    string error = JsonString(payload, "error");
                    throw new InvalidOperationException(error.Length > 0 ? error : BackendFailureMessage(result));
                }
                var items = new List<MailItem>();
                if (payload.TryGetProperty("messages", out JsonElement messages) && messages.ValueKind == JsonValueKind.Array)
                {
                    foreach (JsonElement msg in messages.EnumerateArray())
                    {
                        string received = JsonString(msg, "receivedDateTime");
                        if (received.Length > 19) received = received.Substring(0, 19).Replace("T", " ");
                        items.Add(new MailItem
                        {
                            ReceivedAt = received,
                            From = JsonString(msg, "from"),
                            Subject = JsonString(msg, "subject"),
                            BodyPreview = JsonString(msg, "bodyPreview"),
                            Body = JsonString(msg, "body"),
                            VerificationCode = JsonString(msg, "verificationCode")
                        });
                    }
                }
                return items;
            }
            finally
            {
                foreach (string tempFile in plan.TempFiles)
                    TryDeleteFile(tempFile);
            }
        }

        private static string BackendFailureMessage(BackendCommandResult result)
        {
            string message = string.IsNullOrWhiteSpace(result.StandardError)
                ? SensitiveDataSanitizer.Redact(result.StandardOutput)
                : result.StandardError;
            message = (message ?? "").Trim();
            if (message.Length > 800) message = string.Concat(message.AsSpan(0, 800), "...");
            return message.Length > 0
                ? message
                : $"backend exited with code {result.ExitCode}, but produced no IPC result";
        }

        private bool IsCfWorkerRow(PoolRow row)
        {
            if (row == null) return false;
            return row.MailboxProvider.Equals("cfworker", StringComparison.OrdinalIgnoreCase)
                || row.AccountType.Contains("CFWorker")
                || row.Identifier.EndsWith("@edu.liziai.cloud", StringComparison.OrdinalIgnoreCase)
                || row.Identifier.EndsWith("@liziai.cloud", StringComparison.OrdinalIgnoreCase)
                || row.RawLine.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase);
        }

        private bool IsReMailRow(PoolRow row)
        {
            if (row == null) return false;
            return row.MailboxProvider.Equals("remail", StringComparison.OrdinalIgnoreCase)
                || row.AccountType.Contains("ReMail", StringComparison.OrdinalIgnoreCase);
        }

        private bool IsSmailrRow(PoolRow row)
        {
            if (row == null) return false;
            return row.MailboxProvider.Equals("smailr", StringComparison.OrdinalIgnoreCase)
                || row.AccountType.Contains("Smailr", StringComparison.OrdinalIgnoreCase)
                || row.RawLine.StartsWith("smailr://", StringComparison.OrdinalIgnoreCase);
        }

        private void ShowMailDetailDialog(MailItem item)
        {
            if (item == null) return;
            string content = MailBodyFormatter.ToDisplayText(item.Body, item.BodyPreview);
            if (content.Length == 0) content = Loc.T("（邮件正文为空）");
            string code = item.VerificationCode.Length > 0 ? item.VerificationCode : ExtractVerificationCode(content);
            var dialog = new Window
            {
                Title = item.Subject.Length > 0 ? item.Subject : Loc.T("邮件详情"),
                Owner = this,
                Width = 720,
                Height = 460,
                MinWidth = 560,
                MinHeight = 360,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(14) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var title = new TextBlock
            {
                Text = item.Subject,
                FontSize = (double)FindResource("Type.Subtitle.Size"),
                FontWeight = FontWeights.SemiBold,
                TextWrapping = TextWrapping.Wrap,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain")
            };
            Grid.SetRow(title, 0);
            root.Children.Add(title);

            var meta = new TextBlock
            {
                Text = item.ReceivedAt + "    " + item.From,
                Margin = new Thickness(0, 6, 0, 10),
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub")
            };
            Grid.SetRow(meta, 1);
            root.Children.Add(meta);

            var body = new TextBox
            {
                Text = content,
                IsReadOnly = true,
                AcceptsReturn = true,
                TextWrapping = TextWrapping.Wrap,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
                VerticalContentAlignment = VerticalAlignment.Top,
                Height = double.NaN,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line")
            };
            Grid.SetRow(body, 2);
            root.Children.Add(body);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 10, 0, 0)
            };
            var copyCodeBtn = new Button { Content = code.Length > 0 ? Loc.F("复制验证码 {0}", code) : Loc.T("未识别验证码"), MinWidth = 120, IsEnabled = code.Length > 0 };
            var copyBodyBtn = new Button { Content = Loc.T("复制正文"), Width = 86 };
            var closeBtn = new Button { Content = Loc.T("关闭"), Width = 72 };
            copyCodeBtn.Click += (_, __) =>
            {
                Clipboard.SetText(code);
                Log(Loc.T("验证码已复制：") + code);
            };
            copyBodyBtn.Click += (_, __) => Clipboard.SetText(content);
            closeBtn.Click += (_, __) => dialog.Close();
            actions.Children.Add(copyCodeBtn);
            actions.Children.Add(copyBodyBtn);
            actions.Children.Add(closeBtn);
            Grid.SetRow(actions, 3);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private string ExtractVerificationCode(string text)
        {
            Match match = Regex.Match(text ?? "", @"(?<!\d)\d{5,8}(?!\d)");
            return match.Success ? match.Value : "";
        }

        private sealed class MailItem
        {
            public string ReceivedAt { get; set; } = "";
            public string From { get; set; } = "";
            public string Subject { get; set; } = "";
            public string BodyPreview { get; set; } = "";
            public string Body { get; set; } = "";
            public string VerificationCode { get; set; } = "";
        }
    }
}
