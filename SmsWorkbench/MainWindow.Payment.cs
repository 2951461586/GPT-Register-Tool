namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Payment-link actions and unified protocol extractor
        private void OpenSessions_Click(object sender, RoutedEventArgs e) => OpenPath(GetSessionsDir());

        private void OpenDatabase_Click(object sender, RoutedEventArgs e) => OpenPath(GetDatabasePath());

        private void OpenMailboxPool_Click(object sender, RoutedEventArgs e) => OpenPath(GetMailboxTokenFile());

        private void OpenPayPalLink_Click(object sender, RoutedEventArgs e)
        {
            PoolRow row = SelectedEmailRowOrNotify("打开支付链接");
            if (row == null) return;
            if (string.IsNullOrWhiteSpace(row.PayPalUrl))
            {
                MessageBox.Show("选中账号没有可打开的支付链接。", "无支付链接", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            OpenPayPalUrl(row.PayPalUrl, row.Identifier);
        }

        private void RegeneratePayPalLink_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedEmailRowsOrNotify("重新生成支付链接");
            if (rows.Count == 0) return;
            string paymentMethod = ShowPaymentMethodDialog("重新生成链接", "生链方式");
            if (paymentMethod.Length == 0) return;

            if (rows.Count == 1)
            {
                PoolRow row = rows[0];
                var singleArgs = new List<string> { "--email", row.Identifier, "--regenerate-paypal-link", "--workers", "4" };
                AddSessionFileArg(singleArgs, row);
                singleArgs.Add("--payment-method");
                singleArgs.Add(paymentMethod);
                RunBackend("重新生成支付链接", singleArgs);
                return;
            }

            string emailFile = Path.Combine(Path.GetTempPath(), "paypal_regen_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
            var args = new List<string> { "--regenerate-paypal-link", "--email-file", emailFile, "--workers", "4" };
            args.Add("--payment-method");
            args.Add(paymentMethod);
            RunBackend("批量重新生成支付链接 (" + rows.Count + ")", args);
        }

        private void MarkPayPalComplete_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedEmailRowsOrNotify("标记支付完成");
            if (rows.Count == 0) return;
            MarkPayPalComplete(rows);
        }

        private void MarkPayPalComplete(PoolRow row)
        {
            MarkPayPalComplete(row == null ? new List<PoolRow>() : new List<PoolRow> { row });
        }

        private void MarkPayPalComplete(List<PoolRow> rows)
        {
            rows = (rows ?? new List<PoolRow>())
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            if (rows.Count == 0)
            {
                ShowEmailSelectionRequired("标记支付完成");
                return;
            }

            if (rows.Count == 1)
            {
                PoolRow row = rows[0];
                var singleArgs = new List<string> { "--email", row.Identifier, "--mark-paypal-status", "completed", "--workers", "4" };
                AddSessionFileArg(singleArgs, row);
                RunBackend("标记支付完成", singleArgs);
                return;
            }

            string emailFile = Path.Combine(Path.GetTempPath(), "paypal_completed_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
            var args = new List<string> { "--mark-paypal-status", "completed", "--email-file", emailFile, "--workers", "4" };
            RunBackend("批量标记支付完成 (" + rows.Count + ")", args);
        }

        private void AtExtractBaLink_Click(object sender, RoutedEventArgs e)
        {
            PoolRow selectedAccount = SelectedRowsOrCurrent()
                .Where(row => !string.IsNullOrWhiteSpace(row.Identifier))
                .FirstOrDefault();
            ShowProtocolPaymentDialog(selectedAccount);
        }

        /// <summary>
        /// Unified protocol payment-link extractor.
        /// </summary>
        private void ShowProtocolPaymentDialog(PoolRow selectedAccount = null)
        {
            ProtocolPaymentPreferences preferences = LoadProtocolPaymentPreferences();
            var win = new Window
            {
                Title = "协议支付提链",
                Width = 620,
                Height = 760,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Owner = this,
                ResizeMode = ResizeMode.CanResize,
                Background = (System.Windows.Media.Brush)FindResource("AppBg"),
            };

            var scrollViewer = new ScrollViewer
            {
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            };
            var mainPanel = new StackPanel { Margin = new Thickness(24) };

            // ── 标题 ──────────────────────────────────────────────────────
            mainPanel.Children.Add(new TextBlock
            {
                Text = "协议支付链接提取",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 16),
            });

            if (selectedAccount != null)
            {
                mainPanel.Children.Add(new Border
                {
                    Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                    BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                    BorderThickness = new Thickness(1),
                    Padding = new Thickness(12, 9, 12, 9),
                    Margin = new Thickness(0, 0, 0, 14),
                    CornerRadius = new CornerRadius(6),
                    Child = new TextBlock
                    {
                        Text = "选中账号：" + selectedAccount.Identifier + "\n请选择需要提取的支付链接方式。",
                        Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                        TextWrapping = TextWrapping.Wrap,
                    },
                });
            }

            // ── 支付方式选择 ──────────────────────────────────────────────
            mainPanel.Children.Add(new TextBlock
            {
                Text = "支付方式",
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 4),
            });
            var methodCombo = new ComboBox
            {
                SelectedIndex = 0,
                Margin = new Thickness(0, 0, 0, 12),
            };
            methodCombo.Items.Add(new ComboBoxItem { Content = "PayPal — 美国/全球 BA 授权链接", Tag = "paypal|US" });
            methodCombo.Items.Add(new ComboBoxItem { Content = "GoPay — 印尼协议支付", Tag = "gopay|ID" });
            methodCombo.Items.Add(new ComboBoxItem { Content = "UPI — 印度统一支付接口", Tag = "upi|IN" });
            methodCombo.Items.Add(new ComboBoxItem { Content = "iDEAL — 荷兰银行支付", Tag = "ideal|NL" });
            methodCombo.Items.Add(new ComboBoxItem { Content = "PIX — 巴西即时支付", Tag = "pix|BR" });
            methodCombo.Items.Add(new ComboBoxItem { Content = "Kakao Pay — 韩国钱包支付", Tag = "kakao|KR" });
            methodCombo.Items.Add(new ComboBoxItem { Content = "BLIK — 波兰银行码支付（提交六位码）", Tag = "blik|PL" });
            methodCombo.Items.Add(new ComboBoxItem { Content = "TWINT — 瑞士钱包支付", Tag = "twint|CH" });
            methodCombo.Items.Add(new ComboBoxItem { Content = "直卡 Checkout — 直接刷卡结账链接", Tag = "direct_card|PH" });
            methodCombo.Items.Add(new ComboBoxItem { Content = "MoMo — 越南钱包扫码支付", Tag = "momo|VN" });
            mainPanel.Children.Add(methodCombo);

            // ── AT 输入 ───────────────────────────────────────────────────
            var atLabel = new TextBlock
            {
                Text = "Access Token (JWT)",
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 4),
                Visibility = selectedAccount == null ? Visibility.Visible : Visibility.Collapsed,
            };
            mainPanel.Children.Add(atLabel);
            var atBox = new TextBox
            {
                Height = 80,
                TextWrapping = TextWrapping.Wrap,
                AcceptsReturn = true,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                FontSize = 12,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                Margin = new Thickness(0, 0, 0, 12),
                Visibility = selectedAccount == null ? Visibility.Visible : Visibility.Collapsed,
            };
            mainPanel.Children.Add(atBox);

            // ── 目标国家 ──────────────────────────────────────────────────
            mainPanel.Children.Add(new TextBlock
            {
                Text = "结算国家 (账单区域)",
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 4),
            });
            var countryCombo = new ComboBox
            {
                SelectedIndex = 0,
                Margin = new Thickness(0, 0, 0, 12),
            };
            var countries = new[] {
                "US - 美国", "ID - 印度尼西亚", "IN - 印度", "NL - 荷兰",
                "BR - 巴西", "KR - 韩国", "PL - 波兰", "CH - 瑞士",
                "VN - 越南", "PH - 菲律宾",
                "DE - 德国", "GB - 英国", "JP - 日本", "FR - 法国",
                "AU - 澳大利亚", "SG - 新加坡", "CA - 加拿大", "NZ - 新西兰", "IE - 爱尔兰",
            };
            foreach (var c in countries)
                countryCombo.Items.Add(new ComboBoxItem { Content = c });
            mainPanel.Children.Add(countryCombo);

            // ── 代理配置 ──────────────────────────────────────────────────
            mainPanel.Children.Add(new TextBlock
            {
                Text = "代理 (可选，留空使用配置文件默认值)",
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 4),
            });
            var proxyBox = new TextBox
            {
                Text = preferences.Proxy,
                Height = 28,
                FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                FontSize = 12,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                Margin = new Thickness(0, 0, 0, 4),
            };
            mainPanel.Children.Add(proxyBox);

            ComboBox CreateStageCountryCombo(string selectedCountry)
            {
                var combo = new ComboBox { MinWidth = 145 };
                foreach (var item in new[] {
                    ("US", "美国 US"), ("GB", "英国 GB"), ("DE", "德国 DE"),
                    ("JP", "日本 JP"), ("BR", "巴西 BR"), ("TR", "土耳其 TR"),
                    ("VN", "越南 VN"),
                })
                {
                    combo.Items.Add(new ComboBoxItem { Content = item.Item2, Tag = item.Item1 });
                }
                string wanted = (selectedCountry ?? "").Trim().ToUpperInvariant();
                combo.SelectedIndex = 0;
                for (int index = 0; index < combo.Items.Count; index++)
                {
                    if (combo.Items[index] is ComboBoxItem option
                        && string.Equals(Convert.ToString(option.Tag), wanted, StringComparison.OrdinalIgnoreCase))
                    {
                        combo.SelectedIndex = index;
                        break;
                    }
                }
                return combo;
            }

            var stageProxyPanel = new StackPanel { Margin = new Thickness(0, 8, 0, 12) };
            stageProxyPanel.Children.Add(new TextBlock
            {
                Text = "分段代理目标地区",
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 5),
            });

            var stageGrid = new Grid();
            stageGrid.ColumnDefinitions.Add(new ColumnDefinition());
            stageGrid.ColumnDefinitions.Add(new ColumnDefinition());
            stageGrid.ColumnDefinitions.Add(new ColumnDefinition());
            var checkoutCountryCombo = CreateStageCountryCombo(FirstNonEmpty(preferences.CheckoutCountry, "US"));
            var approveCountryCombo = CreateStageCountryCombo(FirstNonEmpty(preferences.ApproveCountry, "TR"));
            var updateCountryCombo = CreateStageCountryCombo(FirstNonEmpty(preferences.UpdateCountry, "TR"));
            var stageControls = new[]
            {
                ("Checkout", checkoutCountryCombo),
                ("Approve", approveCountryCombo),
                ("Update", updateCountryCombo),
            };
            for (int index = 0; index < stageControls.Length; index++)
            {
                var stageColumn = new StackPanel { Margin = new Thickness(index == 0 ? 0 : 5, 0, index == 2 ? 0 : 5, 0) };
                stageColumn.Children.Add(new TextBlock
                {
                    Text = stageControls[index].Item1,
                    FontSize = 11,
                    Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                    Margin = new Thickness(0, 0, 0, 3),
                });
                stageColumn.Children.Add(stageControls[index].Item2);
                Grid.SetColumn(stageColumn, index);
                stageGrid.Children.Add(stageColumn);
            }
            stageProxyPanel.Children.Add(stageGrid);
            mainPanel.Children.Add(stageProxyPanel);

            var blikCodePanel = new StackPanel { Visibility = Visibility.Collapsed, Margin = new Thickness(0, 0, 0, 12) };
            blikCodePanel.Children.Add(new TextBlock
            {
                Text = "BLIK 六位码",
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 4),
            });
            var blikCodeBox = new TextBox
            {
                MaxLength = 6,
                Height = 28,
                FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
            };
            blikCodePanel.Children.Add(blikCodeBox);
            mainPanel.Children.Add(blikCodePanel);

            // ── 选项 ──────────────────────────────────────────────────────
            var optionPanel = new StackPanel { Orientation = Orientation.Vertical, Margin = new Thickness(0, 0, 0, 16) };
            var zeroCheck = new CheckBox
            {
                Content = "严格要求免费试用 / 0 元金额",
                IsChecked = true,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 6),
            };
            var requireBaCheck = new CheckBox
            {
                Content = "必须返回 PayPal BA 授权 URL",
                IsChecked = true,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 0),
            };
            optionPanel.Children.Add(zeroCheck);
            optionPanel.Children.Add(requireBaCheck);
            mainPanel.Children.Add(optionPanel);

            // ── 结果区域 ──────────────────────────────────────────────────
            mainPanel.Children.Add(new TextBlock
            {
                Text = "结果",
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 4),
            });
            var resultBox = new TextBox
            {
                Height = 120,
                TextWrapping = TextWrapping.Wrap,
                IsReadOnly = true,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                FontSize = 12,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                Margin = new Thickness(0, 0, 0, 12),
            };
            mainPanel.Children.Add(resultBox);

            // ── 按钮面板 ──────────────────────────────────────────────────
            var btnPanel = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            var extractBtn = new Button
            {
                Content = "提取",
                Height = 32,
                MinWidth = 100,
                FontWeight = FontWeights.SemiBold,
                Margin = new Thickness(0, 0, 8, 0),
            };
            var testProxyBtn = new Button
            {
                Content = "测试出口",
                Height = 32,
                MinWidth = 88,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                Margin = new Thickness(0, 0, 8, 0),
            };
            var copyBtn = new Button
            {
                Content = "复制链接",
                Height = 32,
                MinWidth = 80,
                IsEnabled = false,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                Margin = new Thickness(0, 0, 8, 0),
            };
            var openQrBtn = new Button
            {
                Content = "打开二维码",
                Height = 32,
                MinWidth = 80,
                IsEnabled = false,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                Margin = new Thickness(0, 0, 8, 0),
            };
            var closeBtn = new Button
            {
                Content = "关闭",
                Height = 32,
                MinWidth = 60,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
            };
            btnPanel.Children.Add(testProxyBtn);
            btnPanel.Children.Add(extractBtn);
            btnPanel.Children.Add(copyBtn);
            btnPanel.Children.Add(openQrBtn);
            btnPanel.Children.Add(closeBtn);
            mainPanel.Children.Add(btnPanel);

            scrollViewer.Content = mainPanel;
            win.Content = scrollViewer;

            string lastUrl = "";
            string lastQrPath = "";

            string SelectedMethod()
            {
                if (methodCombo.SelectedItem is not ComboBoxItem item) return "paypal";
                string tag = Convert.ToString(item.Tag) ?? "paypal|US";
                return tag.Split('|')[0];
            }

            string ComboCode(ComboBox combo)
            {
                return combo.SelectedItem is ComboBoxItem item
                    ? (Convert.ToString(item.Tag) ?? "").Trim().ToUpperInvariant()
                    : "";
            }

            void AddStageCountryArgs(List<string> args)
            {
                string checkoutStage = ComboCode(checkoutCountryCombo);
                string approveStage = ComboCode(approveCountryCombo);
                string updateStage = ComboCode(updateCountryCombo);
                if (checkoutStage.Length > 0) args.AddRange(new[] { "--checkout-proxy-country", checkoutStage });
                if (approveStage.Length > 0) args.AddRange(new[] { "--approve-proxy-country", approveStage });
                if (updateStage.Length > 0) args.AddRange(new[] { "--update-proxy-country", updateStage });
            }

            void SaveSelection()
            {
                SaveProtocolPaymentPreferences(new ProtocolPaymentPreferences
                {
                    Method = SelectedMethod(),
                    Proxy = proxyBox.Text.Trim(),
                    TargetCountry = countryCombo.SelectedItem is ComboBoxItem targetItem
                        ? (Convert.ToString(targetItem.Content) ?? "").Substring(0, 2)
                        : "US",
                    CheckoutCountry = ComboCode(checkoutCountryCombo),
                    ApproveCountry = ComboCode(approveCountryCombo),
                    UpdateCountry = ComboCode(updateCountryCombo),
                });
            }

            // ── 支付方式切换时更新国家默认值 ──────────────────────────────
            methodCombo.SelectionChanged += (_, __) =>
            {
                string method = SelectedMethod();
                string tag = Convert.ToString((methodCombo.SelectedItem as ComboBoxItem)?.Tag) ?? "paypal|US";
                string[] tagParts = tag.Split('|');
                string defaultCountry = tagParts.Length > 1 ? tagParts[1] : "US";
                for (int index = 0; index < countryCombo.Items.Count; index++)
                {
                    if (countryCombo.Items[index] is ComboBoxItem countryItem && Convert.ToString(countryItem.Content)?.StartsWith(defaultCountry + " ", StringComparison.OrdinalIgnoreCase) == true)
                    {
                        countryCombo.SelectedIndex = index;
                        break;
                    }
                }
                requireBaCheck.IsEnabled = method == "paypal";
                blikCodePanel.Visibility = method == "blik" ? Visibility.Visible : Visibility.Collapsed;
                stageProxyPanel.Visibility = method == "paypal" || method == "upi" || method == "direct_card" || method == "momo" ? Visibility.Visible : Visibility.Collapsed;
                updateCountryCombo.IsEnabled = method == "paypal" || method == "direct_card";
                extractBtn.Content = method == "blik" ? "执行支付" : "提取";
            };
            for (int index = 0; index < methodCombo.Items.Count; index++)
            {
                if (methodCombo.Items[index] is ComboBoxItem item
                    && string.Equals(Convert.ToString(item.Tag)?.Split('|')[0], preferences.Method, StringComparison.OrdinalIgnoreCase))
                {
                    methodCombo.SelectedIndex = index;
                    break;
                }
            }
            if (!string.IsNullOrWhiteSpace(preferences.TargetCountry))
            {
                for (int index = 0; index < countryCombo.Items.Count; index++)
                {
                    if (countryCombo.Items[index] is ComboBoxItem item
                        && Convert.ToString(item.Content)?.StartsWith(preferences.TargetCountry + " ", StringComparison.OrdinalIgnoreCase) == true)
                    {
                        countryCombo.SelectedIndex = index;
                        break;
                    }
                }
            }

            testProxyBtn.Click += async (_, __) =>
            {
                SaveSelection();
                var args = new List<string> { "--test-payment-proxies", "--payment-method", SelectedMethod() };
                string proxy = proxyBox.Text.Trim();
                if (proxy.Length > 0) args.AddRange(new[] { "--proxy", proxy });
                AddStageCountryArgs(args);

                resultBox.Text = "正在测试 checkout / approve / update 代理出口...";
                testProxyBtn.IsEnabled = false;
                extractBtn.IsEnabled = false;
                try
                {
                    string result = await Task.Run(() => RunBackendWithResult("测试协议支付代理", args));
                    using JsonDocument json = JsonDocument.Parse(result);
                    JsonElement root = json.RootElement;
                    var lines = new List<string>();
                    bool allOk = root.TryGetProperty("ok", out JsonElement okEl) && okEl.GetBoolean();
                    lines.Add(allOk ? "[成功] 代理出口符合选择" : "[失败] 存在不可用或地区不匹配的代理");
                    if (root.TryGetProperty("stages", out JsonElement stagesEl) && stagesEl.ValueKind == JsonValueKind.Object)
                    {
                        foreach (string stage in new[] { "checkout", "approve", "update" })
                        {
                            if (!stagesEl.TryGetProperty(stage, out JsonElement stageEl)) continue;
                            string ip = stageEl.TryGetProperty("ip", out JsonElement ipEl) ? ipEl.GetString() ?? "" : "";
                            string actual = stageEl.TryGetProperty("country_code", out JsonElement ccEl) ? ccEl.GetString() ?? "" : "";
                            string expected = stageEl.TryGetProperty("expected_country", out JsonElement expectedEl) ? expectedEl.GetString() ?? "" : "";
                            string error = stageEl.TryGetProperty("error", out JsonElement errorEl) ? errorEl.GetString() ?? "" : "";
                            lines.Add($"{stage}: {ip} / {actual} (目标 {expected})" + (error.Length > 0 ? $" - {error}" : ""));
                        }
                    }
                    resultBox.Text = string.Join(Environment.NewLine, lines);
                }
                catch (Exception ex)
                {
                    resultBox.Text = "[异常] " + ex.Message;
                }
                finally
                {
                    testProxyBtn.IsEnabled = true;
                    extractBtn.IsEnabled = true;
                }
            };

            // ── 提取按钮 ──────────────────────────────────────────────────
            extractBtn.Click += async (_, __) =>
            {
                string at = atBox.Text.Trim();
                if (selectedAccount == null && string.IsNullOrEmpty(at))
                {
                    resultBox.Text = "请输入 Access Token";
                    return;
                }

                string method = SelectedMethod();
                if (method == "blik" && (blikCodeBox.Text.Trim().Length != 6 || !blikCodeBox.Text.Trim().All(char.IsDigit)))
                {
                    resultBox.Text = "请输入有效的 6 位 BLIK Code";
                    return;
                }
                string country = "US";
                if (countryCombo.SelectedItem is ComboBoxItem ci && ci.Content.ToString().Length >= 2)
                    country = ci.Content.ToString().Substring(0, 2);

                string proxy = proxyBox.Text.Trim();
                bool requireZero = zeroCheck.IsChecked == true;
                bool requireBaToken = requireBaCheck.IsChecked == true;
                SaveSelection();

                resultBox.Text = "正在执行 " + PaymentMethodLabel(method) + " 协议提链...";
                extractBtn.IsEnabled = false;
                copyBtn.IsEnabled = false;
                openQrBtn.IsEnabled = false;
                var args = new List<string>();
                string transientSessionFile = "";

                try
                {
                    args.AddRange(new[] { "--extract-payment-link", "--payment-method", method, "--target-country", country });
                    if (selectedAccount != null)
                    {
                        args.AddRange(new[] { "--email", selectedAccount.Identifier });
                        AddSessionFileArg(args, selectedAccount);
                    }
                    else
                    {
                        transientSessionFile = Path.Combine(Path.GetTempPath(), "protocol_payment_at_" + Guid.NewGuid().ToString("N") + ".json");
                        File.WriteAllText(
                            transientSessionFile,
                            JsonSerializer.Serialize(new Dictionary<string, string> { ["access_token"] = at }),
                            new UTF8Encoding(false));
                        args.AddRange(new[] { "--session-file", transientSessionFile });
                    }

                    if (!string.IsNullOrEmpty(proxy))
                        args.AddRange(new[] { "--proxy", proxy });

                    AddStageCountryArgs(args);

                    if (!requireZero)
                        args.Add("--no-require-zero");
                    if (method == "paypal" && requireBaToken)
                        args.Add("--require-ba-token");
                    if (method == "blik" && !string.IsNullOrWhiteSpace(blikCodeBox.Text))
                        args.AddRange(new[] { "--blik-code", blikCodeBox.Text.Trim() });

                    string taskName = PaymentMethodLabel(method) + " 协议提链";
                    var result = await Task.Run(() => RunBackendWithResult(taskName, args, ProtocolPaymentBackendTimeoutMs(method)));

                    // 解析 JSON 结果
                    try
                    {
                        var json = System.Text.Json.JsonDocument.Parse(result);
                        var root = json.RootElement;
                        if (root.TryGetProperty("ok", out var ok) && ok.GetBoolean())
                        {
                            var sb = new StringBuilder();
                            bool paymentCompleted = root.TryGetProperty("status", out var statusEl)
                                && string.Equals(statusEl.GetString(), "completed", StringComparison.OrdinalIgnoreCase);
                            sb.AppendLine(paymentCompleted ? "[成功] 支付已完成" : "[成功] 提取成功!");
                            sb.AppendLine();

                            if (root.TryGetProperty("message", out var messageEl) && !string.IsNullOrWhiteSpace(messageEl.GetString()))
                                sb.AppendLine(messageEl.GetString());

                            // URL / UPI URI
                            string url = "";
                            if (root.TryGetProperty("upi_uri", out var upiUri) && !string.IsNullOrEmpty(upiUri.GetString()))
                            {
                                url = upiUri.GetString() ?? "";
                                sb.AppendLine($"UPI URI: {url}"); // UPI URI 为技术字段名，保留
                            }
                            else if (root.TryGetProperty("url", out var urlEl) && !string.IsNullOrEmpty(urlEl.GetString()))
                            {
                                url = urlEl.GetString() ?? "";
                                sb.AppendLine($"链接: {url}");
                            }

                            if (root.TryGetProperty("hosted_url", out var hostedEl))
                                sb.AppendLine($"托管 URL: {hostedEl.GetString()}");

                            if (root.TryGetProperty("link_type", out var ltEl))
                                sb.AppendLine($"链接类型: {ltEl.GetString()}");

                            if (root.TryGetProperty("run_id", out var runIdEl))
                                sb.AppendLine($"任务 ID: {runIdEl.GetString()}");

                            if (root.TryGetProperty("manager_state", out var stateEl))
                                sb.AppendLine($"状态机: {stateEl.GetString()}");

                            if (root.TryGetProperty("qr_path", out var qrPathEl))
                            {
                                lastQrPath = qrPathEl.GetString() ?? "";
                                if (!string.IsNullOrEmpty(lastQrPath))
                                    sb.AppendLine($"QR 图片: {lastQrPath}");
                            }

                            if (root.TryGetProperty("cs_id", out var csIdEl))
                                sb.AppendLine($"CS ID: {csIdEl.GetString()}"); // CS ID 为 Stripe 字段名，保留

                            if (root.TryGetProperty("amount", out var amtEl))
                                sb.AppendLine($"金额: {amtEl}");

                            if (root.TryGetProperty("currency", out var curEl))
                                sb.AppendLine($"货币: {curEl.GetString()}");

                            if (root.TryGetProperty("coupon_name", out var couponEl))
                            {
                                var couponStr = couponEl.GetString();
                                if (!string.IsNullOrEmpty(couponStr))
                                    sb.AppendLine($"优惠券: {couponStr}");
                            }

                            if (root.TryGetProperty("approval_ok", out var apprEl))
                                sb.AppendLine($"审批状态: {(apprEl.GetBoolean() ? "已批准" : "待处理/失败")}");

                            if (root.TryGetProperty("expires_at", out var expEl))
                            {
                                try
                                {
                                    var expires = expEl.GetInt64();
                                    if (expires > 0)
                                    {
                                        var dt = DateTimeOffset.FromUnixTimeSeconds(expires).LocalDateTime;
                                        sb.AppendLine($"过期时间: {dt:yyyy-MM-dd HH:mm:ss}");
                                    }
                                }
                                catch { }
                            }

                            if (root.TryGetProperty("target_country", out var tcEl))
                                sb.AppendLine($"国家: {tcEl.GetString()}");

                            if (root.TryGetProperty("warning", out var warnEl))
                                sb.AppendLine($"警告: {warnEl.GetString()}");

                            resultBox.Text = sb.ToString().TrimEnd();
                            lastUrl = url;
                            copyBtn.IsEnabled = !string.IsNullOrEmpty(lastUrl);
                            openQrBtn.IsEnabled = !string.IsNullOrEmpty(lastQrPath) && File.Exists(lastQrPath);
                        }
                        else
                        {
                            string error = "";
                            if (root.TryGetProperty("error", out var err))
                                error = err.GetString() ?? "";
                            string errorCode = "";
                            if (root.TryGetProperty("error_code", out var ec))
                                errorCode = ec.GetString() ?? "";
                            resultBox.Text = $"[失败] {error}" + (string.IsNullOrEmpty(errorCode) ? "" : $"\n错误代码: {errorCode}");
                        }
                    }
                    catch
                    {
                        // 非 JSON 结果，直接显示
                        resultBox.Text = result;
                    }
                }
                catch (Exception ex)
                {
                    resultBox.Text = $"[异常] {ex.Message}";
                }
                finally
                {
                    try
                    {
                        if (transientSessionFile.Length > 0)
                            File.Delete(transientSessionFile);
                    }
                    catch { }
                    extractBtn.IsEnabled = true;
                }
            };

            // ── 复制按钮 ──────────────────────────────────────────────────
            copyBtn.Click += (_, __) =>
            {
                if (!string.IsNullOrEmpty(lastUrl))
                {
                    System.Windows.Clipboard.SetText(lastUrl);
                    copyBtn.Content = "已复制!";
                    Task.Delay(1500).ContinueWith(_ => Dispatcher.Invoke(() => copyBtn.Content = "复制链接"));
                }
            };

            // ── 打开 QR 按钮 ─────────────────────────────────────────────
            openQrBtn.Click += (_, __) =>
            {
                if (!string.IsNullOrEmpty(lastQrPath) && File.Exists(lastQrPath))
                {
                    try
                    {
                        System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
                        {
                            FileName = lastQrPath,
                            UseShellExecute = true,
                        });
                    }
                    catch (Exception ex)
                    {
                        MessageBox.Show($"打开 QR 图片失败: {ex.Message}", "错误", MessageBoxButton.OK, MessageBoxImage.Warning);
                    }
                }
            };

            closeBtn.Click += (_, __) =>
            {
                SaveSelection();
                win.Close();
            };
            win.Closed += (_, __) => SaveSelection();

            win.ShowDialog();
        }

        private ProtocolPaymentPreferences LoadProtocolPaymentPreferences()
        {
            string path = ProtocolPaymentPreferencesPath();
            try
            {
                if (File.Exists(path))
                {
                    ProtocolPaymentHistoryFile saved = JsonSerializer.Deserialize<ProtocolPaymentHistoryFile>(
                        File.ReadAllText(path, Encoding.UTF8),
                        new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
                    if (saved?.Last != null)
                    {
                        if (RemoveProtocolPaymentSecrets(saved))
                            File.WriteAllText(path, JsonSerializer.Serialize(saved, new JsonSerializerOptions { WriteIndented = true }), Encoding.UTF8);
                        return saved.Last;
                    }
                }
            }
            catch (Exception ex)
            {
                Log("读取协议支付历史选择失败：" + ex.Message);
            }

            var defaults = new ProtocolPaymentPreferences();
            try
            {
                Dictionary<string, object> config = ReadJsonObject(Path.Combine(rootDir, "config.json"));
                Dictionary<string, object> paypal = GetSection(config, "paypal");
                Dictionary<string, object> countries = GetSection(paypal, "stage_proxy_countries");
                defaults.CheckoutCountry = FirstNonEmpty(GetString(countries, "checkout"), "US");
                defaults.ApproveCountry = FirstNonEmpty(GetString(countries, "approve"), "TR");
                defaults.UpdateCountry = FirstNonEmpty(GetString(countries, "promotion"), "TR");
                defaults.TargetCountry = FirstNonEmpty(GetString(paypal, "target_country"), "US");
            }
            catch
            {
            }
            return defaults;
        }

        private void SaveProtocolPaymentPreferences(ProtocolPaymentPreferences preferences)
        {
            if (preferences == null) return;
            string path = ProtocolPaymentPreferencesPath();
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(path) ?? rootDir);
                ProtocolPaymentHistoryFile saved = null;
                if (File.Exists(path))
                {
                    try
                    {
                        saved = JsonSerializer.Deserialize<ProtocolPaymentHistoryFile>(
                            File.ReadAllText(path, Encoding.UTF8),
                            new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
                    }
                    catch
                    {
                    }
                }
                saved ??= new ProtocolPaymentHistoryFile();
                saved.History ??= new List<ProtocolPaymentHistoryEntry>();
                preferences.Proxy = "";
                RemoveProtocolPaymentSecrets(saved);
                string signature = preferences.Signature();
                if (saved.History.Count == 0 || !string.Equals(saved.History[0].Signature, signature, StringComparison.Ordinal))
                {
                    saved.History.Insert(0, new ProtocolPaymentHistoryEntry
                    {
                        SavedAt = DateTimeOffset.Now.ToString("O"),
                        Signature = signature,
                        Selection = preferences,
                    });
                }
                saved.History = saved.History.Take(20).ToList();
                saved.Last = preferences;
                File.WriteAllText(path, JsonSerializer.Serialize(saved, new JsonSerializerOptions { WriteIndented = true }), Encoding.UTF8);
            }
            catch (Exception ex)
            {
                Log("保存协议支付历史选择失败：" + ex.Message);
            }
        }

        private string ProtocolPaymentPreferencesPath()
        {
            return Path.Combine(rootDir, "runtime", "protocol_payment_history.json");
        }

        private bool RemoveProtocolPaymentSecrets(ProtocolPaymentHistoryFile saved)
        {
            bool changed = false;
            void ClearProxy(ProtocolPaymentPreferences selection)
            {
                if (selection == null || string.IsNullOrEmpty(selection.Proxy)) return;
                selection.Proxy = "";
                changed = true;
            }

            ClearProxy(saved?.Last);
            foreach (ProtocolPaymentHistoryEntry entry in saved?.History ?? new List<ProtocolPaymentHistoryEntry>())
            {
                ClearProxy(entry?.Selection);
                if (entry?.Selection != null)
                    entry.Signature = entry.Selection.Signature();
            }
            return changed;
        }

        private int ProtocolPaymentBackendTimeoutMs(string paymentMethod)
        {
            int seconds = 900;
            try
            {
                Dictionary<string, object> config = ReadJsonObject(Path.Combine(rootDir, "config.json"));
                Dictionary<string, object> protocol = GetSection(config, "protocol_payments");
                if (int.TryParse(GetString(protocol, "timeout_seconds"), out int configured))
                    seconds = configured;
                Dictionary<string, object> methods = GetChildSection(protocol, "methods");
                Dictionary<string, object> method = GetChildSection(methods, NormalizePaymentMethod(paymentMethod));
                if (int.TryParse(GetString(method, "timeout_seconds"), out int methodConfigured))
                    seconds = methodConfigured;
            }
            catch { }
            seconds = Math.Max(30, Math.Min(3600, seconds));
            return (seconds + 30) * 1000;
        }

        private sealed class ProtocolPaymentPreferences
        {
            public string Method { get; set; } = "paypal";
            public string Proxy { get; set; } = "";
            public string TargetCountry { get; set; } = "US";
            public string CheckoutCountry { get; set; } = "US";
            public string ApproveCountry { get; set; } = "TR";
            public string UpdateCountry { get; set; } = "TR";

            public string Signature()
            {
                return string.Join("|", Method, TargetCountry, CheckoutCountry, ApproveCountry, UpdateCountry);
            }
        }

        private sealed class ProtocolPaymentHistoryEntry
        {
            public string SavedAt { get; set; } = "";
            public string Signature { get; set; } = "";
            public ProtocolPaymentPreferences Selection { get; set; } = new ProtocolPaymentPreferences();
        }

        private sealed class ProtocolPaymentHistoryFile
        {
            public ProtocolPaymentPreferences Last { get; set; } = new ProtocolPaymentPreferences();
            public List<ProtocolPaymentHistoryEntry> History { get; set; } = new List<ProtocolPaymentHistoryEntry>();
        }

    }
}
