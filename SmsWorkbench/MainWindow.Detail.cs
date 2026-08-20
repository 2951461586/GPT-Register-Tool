namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Account detail dialog and detail formatting
        private void ShowAccountDetail(PoolRow row)
            => RunUiTask(() => ShowAccountDetailAsync(row));

        private async Task ShowAccountDetailAsync(PoolRow row)
        {
            if (row == null) return;
            string detail = await BuildAccountDetailAsync(row);
            string paypalUrl = row.PayPalUrl ?? "";
            bool hasPayPal = !string.IsNullOrWhiteSpace(paypalUrl);
            string accessToken = await ResolveAccountAccessTokenAsync(row);
            bool hasAccessToken = !string.IsNullOrWhiteSpace(accessToken);
            var dialog = new Window
            {
                Title = "账号详情 - " + row.Identifier,
                Owner = this,
                Width = 960,
                Height = 740,
                MinWidth = 780,
                MinHeight = 580,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(16) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });     // 0: title
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });     // 1: summary
            if (hasPayPal)
                root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto }); // 2: paypal url
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) }); // detail
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });     // actions

            // Title with status badge
            var titlePanel = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 12) };
            titlePanel.Children.Add(new TextBlock
            {
                Text = row.Identifier,
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                VerticalAlignment = VerticalAlignment.Center
            });
            if (!string.IsNullOrWhiteSpace(row.Status))
            {
                titlePanel.Children.Add(new Border
                {
                    Background = (System.Windows.Media.Brush)FindResource("PrimarySoft"),
                    CornerRadius = new CornerRadius(4),
                    Padding = new Thickness(8, 2, 8, 2),
                    Margin = new Thickness(12, 0, 0, 0),
                    VerticalAlignment = VerticalAlignment.Center,
                    Child = new TextBlock
                    {
                        Text = row.Status,
                        FontSize = 11,
                        Foreground = (System.Windows.Media.Brush)FindResource("TextSub")
                    }
                });
            }
            Grid.SetRow(titlePanel, 0);
            root.Children.Add(titlePanel);

            // Summary cards - 2-column layout
            var summaryGrid = new Grid { Margin = new Thickness(0, 0, 0, 12) };
            summaryGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            summaryGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            for (int i = 0; i < 4; i++) summaryGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var infoItems = new (string label, string value)[]
            {
                ("邮箱", row.Identifier),
                ("类型", row.AccountType ?? ""),
                ("状态", row.Status ?? ""),
                ("支付状态", row.PayPalStatus ?? ""),
                ("支付金额", row.PayPalAmount ?? ""),
                ("Refresh Token", row.RefreshTokenStatus ?? ""),
                ("创建时间", row.CreatedAt ?? ""),
                ("更新时间", row.CompletedAt ?? ""),
            };

            int idx = 0;
            for (int r = 0; r < 4; r++)
            {
                for (int c = 0; c < 2 && idx < infoItems.Length; c++, idx++)
                {
                    var (label, value) = infoItems[idx];
                    var card = new Border
                    {
                        Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                        BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                        BorderThickness = new Thickness(1),
                        CornerRadius = new CornerRadius(8),
                        Padding = new Thickness(12, 8, 12, 8),
                        Margin = new Thickness(c == 0 ? 0 : 6, r == 0 ? 0 : 6, c == 1 ? 0 : 6, 0)
                    };
                    var cardStack = new StackPanel();
                    cardStack.Children.Add(new TextBlock
                    {
                        Text = label,
                        FontSize = 11,
                        Foreground = (System.Windows.Media.Brush)FindResource("TextMuted"),
                        Margin = new Thickness(0, 0, 0, 4)
                    });
                    cardStack.Children.Add(new TextBlock
                    {
                        Text = string.IsNullOrWhiteSpace(value) ? "—" : value,
                        FontSize = 13,
                        FontWeight = FontWeights.Medium,
                        Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                        TextTrimming = TextTrimming.CharacterEllipsis
                    });
                    card.Child = cardStack;
                    Grid.SetRow(card, r);
                    Grid.SetColumn(card, c);
                    summaryGrid.Children.Add(card);
                }
            }
            Grid.SetRow(summaryGrid, 1);
            root.Children.Add(summaryGrid);

            // PayPal URL display (if present)
            if (hasPayPal)
            {
                var urlPanel = new Border
                {
                    Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                    BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                    BorderThickness = new Thickness(1),
                    CornerRadius = new CornerRadius(8),
                    Padding = new Thickness(12, 6, 12, 6),
                    Margin = new Thickness(0, 0, 0, 10)
                };
                var urlStack = new StackPanel();
                urlStack.Children.Add(new TextBlock
                {
                    Text = "支付订阅链接",
                    FontSize = 11,
                    Foreground = (System.Windows.Media.Brush)FindResource("TextMuted"),
                    Margin = new Thickness(0, 0, 0, 4)
                });
                urlStack.Children.Add(new TextBox
                {
                    Text = paypalUrl,
                    IsReadOnly = true,
                    TextWrapping = TextWrapping.Wrap,
                    BorderThickness = new Thickness(0),
                    Background = Brushes.Transparent,
                    Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                    FontSize = 12,
                    Padding = new Thickness(0)
                });
                urlPanel.Child = urlStack;
                Grid.SetRow(urlPanel, 2);
                root.Children.Add(urlPanel);
            }

            // Raw detail text
            var detailBorder = new Border
            {
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Margin = new Thickness(0, 0, 0, 10)
            };
            var text = new TextBox
            {
                Text = detail,
                IsReadOnly = true,
                AcceptsReturn = true,
                TextWrapping = TextWrapping.NoWrap,
                FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                FontSize = 12,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                VerticalAlignment = VerticalAlignment.Stretch,
                HorizontalAlignment = HorizontalAlignment.Stretch,
                MinHeight = 200,
                Background = Brushes.Transparent,
                BorderThickness = new Thickness(0),
                Padding = new Thickness(10, 8, 10, 8)
            };
            detailBorder.Child = text;
            int detailRow = hasPayPal ? 3 : 2;
            Grid.SetRow(detailBorder, detailRow);
            root.Children.Add(detailBorder);

            // Action buttons - two rows for better spacing
            var actionsGrid = new Grid { Margin = new Thickness(0, 4, 0, 0) };
            actionsGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            actionsGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

            // Left: secondary actions
            var leftActions = new StackPanel { Orientation = Orientation.Horizontal };
            var openButton = new Button { Content = "打开源文件", MinWidth = 100, Margin = new Thickness(0, 0, 8, 0) };
            openButton.Click += (_, __) => OpenAccountJson(row);
            leftActions.Children.Add(openButton);

            var copyAtButton = new Button { Content = "一键复制AT", MinWidth = 100, IsEnabled = hasAccessToken, Margin = new Thickness(0, 0, 8, 0) };
            copyAtButton.Click += (_, __) => RunUiTask(async () =>
            {
                if (!hasAccessToken) return;
                Clipboard.SetText(accessToken);
                copyAtButton.Content = "已复制";
                await Task.Delay(1200);
                copyAtButton.Content = "一键复制AT";
            });
            leftActions.Children.Add(copyAtButton);

            var checkAliveButton = new Button { Content = "账号测活", MinWidth = 100, Margin = new Thickness(0, 0, 8, 0) };
            checkAliveButton.Click += (_, __) => RunUiTask(async () =>
            {
                dialog.Close();
                await CheckAccountAliveAsync(row);
            });
            leftActions.Children.Add(checkAliveButton);

            // Right: primary actions
            var rightActions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            var openPayPalButton = new Button { Content = "打开支付链接", MinWidth = 120, IsEnabled = hasPayPal, Margin = new Thickness(0, 0, 8, 0) };
            openPayPalButton.Click += (_, __) => OpenPayPalUrl(paypalUrl, row.Identifier);
            var copyPayPalButton = new Button { Content = "复制支付链接", MinWidth = 120, IsEnabled = hasPayPal, Margin = new Thickness(0, 0, 8, 0) };
            copyPayPalButton.Click += (_, __) => CopyPayPalUrl(paypalUrl, row.Identifier);
            var markPayPalCompleteButton = new Button { Content = "标记支付完成", MinWidth = 120, Style = (System.Windows.Style)FindResource("PrimaryButton"), Margin = new Thickness(0, 0, 8, 0) };
            markPayPalCompleteButton.Click += (_, __) =>
            {
                MarkPayPalComplete(row);
                dialog.Close();
            };
            var closeButton = new Button { Content = "关闭", MinWidth = 80 };
            closeButton.Click += (_, __) => dialog.Close();
            rightActions.Children.Add(openPayPalButton);
            rightActions.Children.Add(copyPayPalButton);
            rightActions.Children.Add(markPayPalCompleteButton);
            rightActions.Children.Add(closeButton);

            Grid.SetColumn(leftActions, 0);
            Grid.SetColumn(rightActions, 1);
            actionsGrid.Children.Add(leftActions);
            actionsGrid.Children.Add(rightActions);
            Grid.SetRow(actionsGrid, detailRow + 1);
            root.Children.Add(actionsGrid);

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private async Task<string> ResolveAccountAccessTokenAsync(PoolRow row)
        {
            var data = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            if (!await TryLoadAccountDataForRowAsync(row, data) || data.Count == 0)
            {
                return "";
            }
            return FirstNonEmpty(
                GetString(data, "access_token"),
                GetString(data, "accessToken"),
                NestedString(data, "auth_session", "access_token"),
                NestedString(data, "auth_session", "accessToken"),
                NestedString(data, "session", "access_token"),
                NestedString(data, "session", "accessToken")
            );
        }

        private void OpenAccountJson(PoolRow row)
            => RunUiTask(() => OpenAccountJsonAsync(row));

        private async Task OpenAccountJsonAsync(PoolRow row)
        {
            string path = await ResolveAccountJsonPathAsync(row);
            if (string.IsNullOrWhiteSpace(path))
            {
                MessageBox.Show("未找到该账号对应的 JSON 文件。", "打开源文件", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            OpenPath(path);
        }

        private async Task<string> ResolveAccountJsonPathAsync(PoolRow row)
        {
            if (row == null) return "";
            string notes = (row.Notes ?? "").Trim();
            if (File.Exists(notes) && notes.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) return notes;
            string source = (row.SourcePath ?? "").Trim();
            if (File.Exists(source) && source.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) return source;
            if (!File.Exists(source) || !source.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase)) return "";

            try
            {
                JsonElement payload = await desktopRead.ReadAccountAsync(OnlyDigits(row.RawLine), row.Identifier);
                if (!payload.TryGetProperty("account", out JsonElement account)) return "";
                Dictionary<string, object> data = JsonElementToDictionary(account);
                string jsonPath = GetString(data, "json_path");
                if (File.Exists(jsonPath) && jsonPath.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) return jsonPath;
                string rawJson = data.TryGetValue("session", out object session) ? JsonSerializer.Serialize(session) : "";
                if (string.IsNullOrWhiteSpace(rawJson)) return "";
                string email = GetString(data, "email");
                string safeEmail = Regex.Replace((email ?? "unknown").Trim(), @"[^a-zA-Z0-9_.@+-]+", "_");
                string dir = Path.Combine(rootDir, "runtime", "account_json");
                Directory.CreateDirectory(dir);
                string outPath = Path.Combine(dir, "account_" + safeEmail + ".json");
                File.WriteAllText(outPath, PrettyJson(rawJson), new UTF8Encoding(false));
                return outPath;
            }
            catch (Exception ex)
            {
                Log("打开账号JSON失败：" + ex.Message);
                return "";
            }
        }

        private string PrettyJson(string rawJson)
        {
            try
            {
                using JsonDocument document = JsonDocument.Parse(rawJson);
                return JsonSerializer.Serialize(document.RootElement, new JsonSerializerOptions { WriteIndented = true });
            }
            catch
            {
                return rawJson;
            }
        }

        private void AddDetailRow(Grid parent, int row, string label, string value)
        {
            var labelBlock = new TextBlock
            {
                Text = label,
                Margin = new Thickness(10, 7, 10, 7),
                VerticalAlignment = VerticalAlignment.Top,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub")
            };
            Grid.SetRow(labelBlock, row);
            Grid.SetColumn(labelBlock, 0);
            parent.Children.Add(labelBlock);

            bool longValue = label.Contains("链接") || (value ?? "").StartsWith("http", StringComparison.OrdinalIgnoreCase);
            var valueBox = new TextBox
            {
                Text = value ?? "",
                Margin = new Thickness(0, 4, 10, 4),
                IsReadOnly = true,
                BorderThickness = longValue ? new Thickness(1) : new Thickness(0),
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                TextWrapping = longValue ? TextWrapping.Wrap : TextWrapping.NoWrap,
                HorizontalScrollBarVisibility = longValue ? ScrollBarVisibility.Disabled : ScrollBarVisibility.Auto,
                VerticalScrollBarVisibility = longValue ? ScrollBarVisibility.Auto : ScrollBarVisibility.Disabled,
                MinHeight = longValue ? 58 : 0,
                MaxHeight = longValue ? 96 : double.PositiveInfinity,
                Padding = longValue ? new Thickness(6, 4, 6, 4) : new Thickness(0)
            };
            Grid.SetRow(valueBox, row);
            Grid.SetColumn(valueBox, 1);
            parent.Children.Add(valueBox);
        }

        private async Task<string> BuildAccountDetailAsync(PoolRow row)
        {
            var lines = new List<string>
            {
                "email: " + row.Identifier,
                "type: " + row.AccountType,
                "status: " + row.Status,
                "created_at: " + row.CreatedAt,
                "updated_at: " + row.CompletedAt,
                "source: " + row.Notes,
                ""
            };

            try
            {
                if (row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase))
                {
                    JsonElement payload = await desktopRead.ReadAccountAsync(OnlyDigits(row.RawLine), row.Identifier);
                    if (payload.TryGetProperty("account", out JsonElement account))
                    {
                        foreach (KeyValuePair<string, object> item in JsonElementToDictionary(account))
                        {
                            lines.Add(item.Key + ": " + MaskSensitiveField(item.Key, Convert.ToString(item.Value) ?? ""));
                        }
                    }
                    return string.Join(Environment.NewLine, lines);
                }
            }
            catch (Exception ex)
            {
                lines.Add("detail_error: " + ex.Message);
            }
            return string.Join(Environment.NewLine, lines);
        }

        private string MaskSensitiveField(string key, string value)
        {
            string lower = (key ?? "").ToLowerInvariant();
            if (lower.Contains("token") || lower.Contains("cookie") || lower.Contains("password") || lower.Contains("api_key"))
            {
                return Mask(value);
            }
            return value ?? "";
        }

    }
}
