namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Inbox view and mail detail dialog
        private async void ShowInboxDialog(PoolRow row)
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

            async Task LoadEmails()
            {
                if (IsCfWorkerRow(row))
                {
                    header.Text = "正在获取 CFWorker 邮件...";
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
                        Log("CFWorker收件箱获取失败：" + ex.Message);
                    }
                    return;
                }

                header.Text = "正在刷新令牌...";
                string tokenUrl = "https://login.microsoftonline.com/common/oauth2/v2.0/token";
                var tokenBody = new Dictionary<string, string>
                {
                    ["grant_type"] = "refresh_token",
                    ["client_id"] = row.ClientId,
                    ["refresh_token"] = row.RawRefreshToken,
                    ["scope"] = "https://graph.microsoft.com/.default offline_access"
                };

                try
                {
                    mailItems.Clear();
                    foreach (MailItem item in await FetchBackendInbox(row, 20))
                    {
                        mailItems.Add(item);
                    }
                    header.Text = row.Identifier + " - " + mailItems.Count + " messages";

                    if (mailItems.Count < 0)
                    {
                    var tokenResp = await httpClient.PostAsync(tokenUrl, new FormUrlEncodedContent(tokenBody));
                    string tokenJson = await tokenResp.Content.ReadAsStringAsync();
                    if (!tokenResp.IsSuccessStatusCode)
                    {
                        header.Text = "令牌刷新失败 (" + (int)tokenResp.StatusCode + ")";
                        Log("收件箱令牌刷新失败：" + tokenJson);
                        return;
                    }

                    using var tokenDoc = JsonDocument.Parse(tokenJson);
                    string accessToken = tokenDoc.RootElement.GetProperty("access_token").GetString() ?? "";

                    header.Text = "正在获取邮件...";
                    string mailUrl = "https://graph.microsoft.com/v1.0/me/messages?$top=20&$orderby=receivedDateTime desc&$select=receivedDateTime,from,subject,bodyPreview";
                    var request = new HttpRequestMessage(HttpMethod.Get, mailUrl);
                    request.Headers.Authorization = new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", accessToken);
                    var mailResp = await httpClient.SendAsync(request);
                    string mailJson = await mailResp.Content.ReadAsStringAsync();

                    if (!mailResp.IsSuccessStatusCode)
                    {
                        header.Text = "获取邮件失败 (" + (int)mailResp.StatusCode + ")";
                        Log("收件箱获取失败：" + mailJson);
                        return;
                    }

                    mailItems.Clear();
                    using var mailDoc = JsonDocument.Parse(mailJson);
                    if (mailDoc.RootElement.TryGetProperty("value", out JsonElement values))
                    {
                        foreach (JsonElement msg in values.EnumerateArray())
                        {
                            string received = msg.TryGetProperty("receivedDateTime", out JsonElement dt) ? dt.GetString() ?? "" : "";
                            string from = "";
                            if (msg.TryGetProperty("from", out JsonElement fromObj) &&
                                fromObj.TryGetProperty("emailAddress", out JsonElement addr) &&
                                addr.TryGetProperty("address", out JsonElement addrStr))
                            {
                                from = addrStr.GetString() ?? "";
                            }
                            string subject = msg.TryGetProperty("subject", out JsonElement subj) ? subj.GetString() ?? "" : "";
                            string preview = msg.TryGetProperty("bodyPreview", out JsonElement bp) ? bp.GetString() ?? "" : "";

                            if (received.Length > 19) received = received.Substring(0, 19).Replace("T", " ");
                            mailItems.Add(new MailItem { ReceivedAt = received, From = from, Subject = subject, BodyPreview = preview });
                        }
                    }
                    header.Text = row.Identifier + " - 最近 " + mailItems.Count + " 封邮件";
                    }
                }
                catch (Exception ex)
                {
                    header.Text = "加载失败：" + ex.Message;
                    Log("收件箱加载异常：" + ex.Message);
                }
            }

            refreshBtn.Click += async (_, __) => await LoadEmails();
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

        private async Task<List<MailItem>> FetchBackendInbox(PoolRow row, int limit)
        {
            string script = Path.Combine(rootDir, "chatgpt_phone_reg.py");
            if (!File.Exists(script)) throw new FileNotFoundException("Backend script not found", script);
            var args = new List<string> { "--view-inbox", "--email", row.Identifier, "--inbox-limit", limit.ToString() };
            string mailboxLine = FindMailboxLineForRow(row);
            if (mailboxLine.Length == 0 && MailboxArgForLine(row.RawLine).Length > 0)
            {
                mailboxLine = row.RawLine;
            }
            string mailboxArg = MailboxArgForLine(mailboxLine);
            string tempMailboxFile = "";
            if (mailboxArg.Length > 0)
            {
                tempMailboxFile = Path.Combine(Path.GetTempPath(), "view_inbox_mailbox_" + DateTime.Now.ToString("yyyyMMdd_HHmmss_fff") + ".txt");
                File.WriteAllText(tempMailboxFile, mailboxLine.Trim() + Environment.NewLine, new UTF8Encoding(false));
                args.AddRange(new[] { mailboxArg, tempMailboxFile });
            }
            AddSessionFileArg(args, row);
            AddProxy(args);
            var psi = new ProcessStartInfo
            {
                FileName = "python",
                Arguments = Quote(script) + " " + JoinArgs(args),
                WorkingDirectory = rootDir,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
            using var process = new Process { StartInfo = psi };
            process.Start();
            string stdout = await process.StandardOutput.ReadToEndAsync();
            string stderr = await process.StandardError.ReadToEndAsync();
            await process.WaitForExitAsync();
            using JsonDocument doc = ParseInboxBackendJson(stdout, stderr, process.ExitCode);
            if (!doc.RootElement.TryGetProperty("ok", out JsonElement ok) || !ok.GetBoolean())
            {
                string error = JsonString(doc.RootElement, "error");
                throw new InvalidOperationException(error.Length > 0 ? error : stdout.Trim());
            }
            var items = new List<MailItem>();
            if (doc.RootElement.TryGetProperty("messages", out JsonElement messages) && messages.ValueKind == JsonValueKind.Array)
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
                        BodyPreview = JsonString(msg, "bodyPreview")
                    });
                }
            }
            return items;
        }

        private JsonDocument ParseInboxBackendJson(string stdout, string stderr, int exitCode)
        {
            string text = stdout ?? "";
            if (TryExtractInboxBackendJson(text, out JsonDocument parsed))
            {
                return parsed;
            }

            string errorText = ((stdout ?? "").Trim() + "\n" + (stderr ?? "").Trim()).Trim();
            if (errorText.Length > 800)
            {
                errorText = errorText.Substring(0, 800) + "...";
            }
            if (errorText.Length == 0)
            {
                errorText = $"backend exited with code {exitCode}, but produced no JSON output";
            }
            throw new InvalidOperationException("后端收件箱输出不是纯 JSON，且未找到可解析的结果对象：" + errorText);
        }

        private bool TryExtractInboxBackendJson(string output, out JsonDocument doc)
        {
            doc = null;
            string text = output ?? "";
            for (int end = text.LastIndexOf('}'); end >= 0; end = end > 0 ? text.LastIndexOf('}', end - 1) : -1)
            {
                for (int start = text.LastIndexOf('{', end); start >= 0; start = start > 0 ? text.LastIndexOf('{', start - 1) : -1)
                {
                    string candidate = text.Substring(start, end - start + 1);
                    try
                    {
                        JsonDocument parsed = JsonDocument.Parse(candidate);
                        if (parsed.RootElement.ValueKind == JsonValueKind.Object
                            && parsed.RootElement.TryGetProperty("ok", out _)
                            && (parsed.RootElement.TryGetProperty("messages", out _)
                                || parsed.RootElement.TryGetProperty("error", out _)
                                || parsed.RootElement.TryGetProperty("provider", out _)))
                        {
                            doc = parsed;
                            return true;
                        }
                        parsed.Dispose();
                    }
                    catch (JsonException)
                    {
                    }
                }
            }
            return false;
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

        private string JsonStringAny(JsonElement obj, params string[] properties)
        {
            if (obj.ValueKind != JsonValueKind.Object) return obj.ValueKind == JsonValueKind.String ? obj.GetString() ?? "" : "";
            foreach (string property in properties)
            {
                if (!obj.TryGetProperty(property, out JsonElement value)) continue;
                if (value.ValueKind == JsonValueKind.String) return value.GetString() ?? "";
                if (value.ValueKind == JsonValueKind.Number) return value.ToString();
            }
            return "";
        }

        private void ShowMailDetailDialog(MailItem item)
        {
            if (item == null) return;
            string code = ExtractVerificationCode(item.BodyPreview);
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
                Text = item.BodyPreview,
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
            copyBodyBtn.Click += (_, __) => Clipboard.SetText(item.BodyPreview);
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
        }
    }
}
