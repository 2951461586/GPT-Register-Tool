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
                Title = "收件箱 - " + row.Identifier,
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
                Text = "正在加载收件箱...",
                FontSize = 14,
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
            mailGrid.Columns.Add(new DataGridTextColumn { Header = "时间", Binding = new System.Windows.Data.Binding("ReceivedAt"), Width = 150 });
            mailGrid.Columns.Add(new DataGridTextColumn { Header = "发件人", Binding = new System.Windows.Data.Binding("From"), Width = 200 });
            mailGrid.Columns.Add(new DataGridTextColumn { Header = "主题", Binding = new System.Windows.Data.Binding("Subject"), Width = new DataGridLength(1, DataGridLengthUnitType.Star) });
            Grid.SetRow(mailGrid, 1);
            root.Children.Add(mailGrid);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 8, 0, 0)
            };
            var refreshBtn = new Button { Content = "刷新", Width = 72 };
            var closeBtn = new Button { Content = "关闭", Width = 72 };
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
                    header.Text = IsReMailRow(row) ? "正在获取 ReMail 邮件..." : IsSmailrRow(row) ? "正在获取 Smailr 邮件..." : "正在获取 CFWorker 邮件...";
                    try
                    {
                        mailItems.Clear();
                        foreach (MailItem item in await FetchBackendInbox(row, 25))
                        {
                            mailItems.Add(item);
                        }
                        header.Text = row.Identifier + " - 最近 " + mailItems.Count + " 封邮件";
                    }
                    catch (Exception ex)
                    {
                        header.Text = "获取邮件失败：" + ex.Message;
                        Log((IsReMailRow(row) ? "ReMail" : IsSmailrRow(row) ? "Smailr" : "CFWorker") + "收件箱获取失败：" + ex.Message);
                    }
                    return;
                }

                header.Text = "正在刷新令牌...";
                try
                {
                    mailItems.Clear();
                    foreach (MailItem item in await FetchBackendInbox(row, 20))
                    {
                        mailItems.Add(item);
                    }
                    header.Text = row.Identifier + " - " + mailItems.Count + " messages";
                }
                catch (Exception ex)
                {
                    header.Text = "加载失败：" + ex.Message;
                    Log("收件箱加载异常：" + ex.Message);
                }
            }

            refreshBtn.Click += (_, __) => RunUiTask(() => LoadEmails());
            mailGrid.MouseDoubleClick += (_, __) =>
            {
                if (mailGrid.SelectedItem is MailItem item)
                {
                    ShowMailDetailDialog(item);
                }
            };

            dialog.Content = root;
            dialog.Show();
            await LoadEmails();
        }

        private async Task<List<MailItem>> FetchBackendInbox(PoolRow row, int limit, CancellationToken ct = default)
        {
            var args = new List<string> { "--desktop-ipc", "--view-inbox", "--email", row.Identifier, "--inbox-limit", limit.ToString(CultureInfo.InvariantCulture) };
            string remailToken = IsReMailRow(row) ? (row.MailboxToken ?? "").Trim() : "";
            string mailboxLine = FindMailboxLineForRow(row);
            if (mailboxLine.Length == 0 && MailboxArgForLine(row.RawLine).Length > 0)
            {
                mailboxLine = row.RawLine;
            }
            string mailboxArg = MailboxArgForLine(mailboxLine);
            string tempMailboxFile = "";
            if (mailboxArg.Length > 0)
            {
                tempMailboxFile = Path.Combine(Path.GetTempPath(), "view_inbox_mailbox_" + DateTime.Now.ToString("yyyyMMdd_HHmmss_fff", CultureInfo.InvariantCulture) + ".txt");
                File.WriteAllText(tempMailboxFile, mailboxLine.Trim() + Environment.NewLine, new UTF8Encoding(false));
                args.AddRange(new[] { mailboxArg, tempMailboxFile });
            }
            AddSessionFileArg(args, row);
            AddMailboxProxy(args);
            var environment = new Dictionary<string, string>();
            if (remailToken.Length > 0)
                environment["REMAIL_SERVICE_TOKEN"] = remailToken;
            try
            {
                BackendCommandResult result = await backendClient.RunAsync(
                    BackendCommand.Create("查看收件箱", args, 120000, environment));
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
                if (tempMailboxFile.Length > 0)
                    TryDeleteFile(tempMailboxFile);
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
            if (content.Length == 0) content = "（邮件正文为空）";
            string code = item.VerificationCode.Length > 0 ? item.VerificationCode : ExtractVerificationCode(content);
            var dialog = new Window
            {
                Title = item.Subject.Length > 0 ? item.Subject : "邮件详情",
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
                FontSize = 16,
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
            var copyCodeBtn = new Button { Content = code.Length > 0 ? "复制验证码 " + code : "未识别验证码", MinWidth = 120, IsEnabled = code.Length > 0 };
            var copyBodyBtn = new Button { Content = "复制正文", Width = 86 };
            var closeBtn = new Button { Content = "关闭", Width = 72 };
            copyCodeBtn.Click += (_, __) =>
            {
                Clipboard.SetText(code);
                Log("验证码已复制：" + code);
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
