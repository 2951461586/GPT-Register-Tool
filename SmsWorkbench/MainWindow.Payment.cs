namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Payment-link actions and unified protocol extractor.
        // CLI argument construction is delegated to BackendCommandPlanner;
        // backend JSON interpretation is delegated to ProtocolPaymentResultPresenter
        // and BackendResultInterpreter.

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
                var plan = BackendCommandPlanner.CreateMarkPaymentComplete(
                    row.Identifier,
                    SessionFileFor(row));
                RunBackend(plan.TaskName, plan.Arguments.ToList());
                return;
            }

            var batchPlan = BackendCommandPlanner.CreateMarkPaymentCompleteBatch(
                rows.Select(r => r.Identifier.Trim()).ToList());
            RunBackend(batchPlan.TaskName, batchPlan.Arguments.ToList());
        }

        private void AtExtractBaLink_Click(object sender, RoutedEventArgs e)
        {
            var selected = SelectedRowsOrCurrent()
                .Where(row => !string.IsNullOrWhiteSpace(row.Identifier))
                .GroupBy(row => row.Identifier.Trim().ToLowerInvariant())
                .Select(group => group.First())
                .ToList();
            if (selected.Count > 1)
            {
                ShowPaymentBatchDialog(selected);
                return;
            }
            ShowProtocolPaymentDialog(selected.FirstOrDefault());
        }

        /// <summary>
        /// Unified protocol payment-link extractor.
        /// Uses ProtocolPaymentExecutionPlanner for CLI construction and
        /// ProtocolPaymentResultPresenter for JSON interpretation.
        /// Error handling is unified via BackendResultInterpreter.
        /// </summary>
        private void ShowProtocolPaymentDialog(PoolRow selectedAccount = null)
        {
            ProtocolPaymentPreferences preferences = LoadProtocolPaymentPreferences();
            var win = new Window
            {
                Title = "协议支付",
                Width = 760,
                Height = 940,
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

            AddProtocolDialogHeader(mainPanel, selectedAccount);

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
            foreach (PaymentMethodDefinition method in PaymentMethods.All)
            {
                methodCombo.Items.Add(new ComboBoxItem
                {
                    Content = method.SingleAccountDescription,
                    Tag = method.Id + "|" + method.DefaultCountry
                });
            }
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
            foreach (PaymentProxyCountryOption country in PaymentMethods.BillingCountryOptions)
                countryCombo.Items.Add(new ComboBoxItem { Content = country.DisplayName });
            mainPanel.Children.Add(countryCombo);

            // ── 代理配置 ──────────────────────────────────────────────────
            var proxyPoolGrid = new Grid { Margin = new Thickness(0, 0, 0, 8) };
            proxyPoolGrid.ColumnDefinitions.Add(new ColumnDefinition());
            proxyPoolGrid.ColumnDefinitions.Add(new ColumnDefinition());

            TextBox CreateProxyPoolBox()
                => new TextBox
                {
                    Height = 84,
                    AcceptsReturn = true,
                    TextWrapping = TextWrapping.NoWrap,
                    VerticalContentAlignment = VerticalAlignment.Top,
                    HorizontalContentAlignment = HorizontalAlignment.Left,
                    Padding = new Thickness(8, 6, 8, 6),
                    VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                    HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                    FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                    FontSize = 12,
                    Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                    Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                    BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                };

            var checkoutProxyPoolBox = CreateProxyPoolBox();
            var approveProxyPoolBox = CreateProxyPoolBox();
            var checkoutProxyColumn = new StackPanel { Margin = new Thickness(0, 0, 6, 0) };
            checkoutProxyColumn.Children.Add(new TextBlock
            {
                Text = "Checkout 代理池（host:port:user:password；支持 http/https/socks5/socks5h）",
                TextWrapping = TextWrapping.Wrap,
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 4),
            });
            checkoutProxyColumn.Children.Add(checkoutProxyPoolBox);
            var approveProxyColumn = new StackPanel { Margin = new Thickness(6, 0, 0, 0) };
            approveProxyColumn.Children.Add(new TextBlock
            {
                Text = "Approve / Update 代理池（host:port:user:password；支持 http/https/socks5/socks5h）",
                TextWrapping = TextWrapping.Wrap,
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 4),
            });
            approveProxyColumn.Children.Add(approveProxyPoolBox);
            Grid.SetColumn(approveProxyColumn, 1);
            proxyPoolGrid.Children.Add(checkoutProxyColumn);
            proxyPoolGrid.Children.Add(approveProxyColumn);
            mainPanel.Children.Add(proxyPoolGrid);

            ComboBox CreateStageCountryCombo(string selectedCountry)
            {
                var combo = new ComboBox { MinWidth = 145 };
                foreach (PaymentProxyCountryOption item in PaymentMethods.BillingCountryOptions)
                {
                    combo.Items.Add(new ComboBoxItem { Content = item.DisplayName, Tag = item.Code });
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
            var jitRefreshCheck = new CheckBox
            {
                Content = "AT 401 自动恢复（RT/Cookie/浏览器/OAuth）",
                IsChecked = true,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 6),
                Visibility = selectedAccount == null ? Visibility.Collapsed : Visibility.Visible,
            };
            var probeOnlyCheck = new CheckBox
            {
                Content = "仅能力探测（Checkout + Stripe init）",
                IsChecked = false,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 6),
                Visibility = selectedAccount == null ? Visibility.Collapsed : Visibility.Visible,
            };
            optionPanel.Children.Add(jitRefreshCheck);
            optionPanel.Children.Add(probeOnlyCheck);
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
            var extractBtn = BuildProtocolDialogButton("提取", 100, primary: true);
            var testProxyBtn = BuildProtocolDialogButton("测试出口", 88);
            var saveProxyBtn = BuildProtocolDialogButton("保存代理", 88);
            var copyBtn = BuildProtocolDialogButton("复制链接", 80, enabled: false);
            var openQrBtn = BuildProtocolDialogButton("打开二维码", 80, enabled: false);
            var cancelBtn = BuildProtocolDialogButton("取消", 60, enabled: false);
            var closeBtn = BuildProtocolDialogButton("关闭", 60, rightMargin: 0);
            btnPanel.Children.Add(testProxyBtn);
            btnPanel.Children.Add(saveProxyBtn);
            btnPanel.Children.Add(extractBtn);
            btnPanel.Children.Add(copyBtn);
            btnPanel.Children.Add(openQrBtn);
            btnPanel.Children.Add(cancelBtn);
            btnPanel.Children.Add(closeBtn);
            mainPanel.Children.Add(btnPanel);

            scrollViewer.Content = mainPanel;
            win.Content = scrollViewer;

            string lastUrl = "";
            string lastQrPath = "";
            CancellationTokenSource executionCancellation = null;
            bool closeAfterCancellation = false;

            string SelectedMethod()
            {
                if (methodCombo.SelectedItem is not ComboBoxItem item) return "paypal";
                string tag = Convert.ToString(item.Tag) ?? "paypal|US";
                return tag.Split('|')[0];
            }

            void UpdateActionButton()
            {
                if (probeOnlyCheck.IsChecked == true)
                {
                    extractBtn.Content = "开始探测";
                    return;
                }
                string method = SelectedMethod();
                if (method == "blik")
                {
                    extractBtn.Content = "执行支付";
                    return;
                }
                extractBtn.Content = "提取";
            }

            string ComboCode(ComboBox combo)
            {
                return combo.SelectedItem is ComboBoxItem item
                    ? (Convert.ToString(item.Tag) ?? "").Trim().ToUpperInvariant()
                    : "";
            }

            void SelectComboCode(ComboBox combo, string country)
            {
                for (int index = 0; index < combo.Items.Count; index++)
                {
                    if (combo.Items[index] is ComboBoxItem item
                        && string.Equals(Convert.ToString(item.Tag), country, StringComparison.OrdinalIgnoreCase))
                    {
                        combo.SelectedIndex = index;
                        return;
                    }
                }
            }

            void LoadMethodProxyConfiguration(bool loadCountries)
            {
                PaymentBatchProxyConfiguration configured = paymentBatchService.LoadProxyConfiguration(SelectedMethod());
                checkoutProxyPoolBox.Text = configured.CheckoutProxyPool ?? "";
                approveProxyPoolBox.Text = configured.ApproveProxyPool ?? "";
                if (!loadCountries) return;
                SelectComboCode(checkoutCountryCombo, configured.CheckoutCountry);
                SelectComboCode(approveCountryCombo, configured.ApproveCountry);
                SelectComboCode(updateCountryCombo, configured.UpdateCountry);
            }

            SettingsSaveResult SaveMethodProxyConfiguration()
                => paymentBatchService.SaveProxyConfiguration(
                    SelectedMethod(),
                    new PaymentBatchProxyConfiguration(
                        checkoutProxyPoolBox.Text,
                        approveProxyPoolBox.Text,
                        ComboCode(checkoutCountryCombo),
                        ComboCode(approveCountryCombo),
                        ComboCode(updateCountryCombo)));

            void SaveSelection()
            {
                SaveProtocolPaymentPreferences(new ProtocolPaymentPreferences
                {
                    Method = SelectedMethod(),
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
                if (method != "paypal")
                {
                    SelectComboCode(checkoutCountryCombo, defaultCountry);
                    SelectComboCode(approveCountryCombo, defaultCountry);
                    SelectComboCode(updateCountryCombo, PaymentMethods.DefaultUpdateCountry(method, defaultCountry));
                }
                LoadMethodProxyConfiguration(loadCountries: true);
                requireBaCheck.IsEnabled = method == "paypal";
                blikCodePanel.Visibility = method == "blik" ? Visibility.Visible : Visibility.Collapsed;
                stageProxyPanel.Visibility = method == "paypal" || method == "gopay" || method == "gcash" || method == "grabpay" || method == "upi" || method == "direct_card" || method == "momo" ? Visibility.Visible : Visibility.Collapsed;
                updateCountryCombo.IsEnabled = method == "paypal" || method == "gopay" || method == "direct_card";
                zeroCheck.IsChecked = true;
                zeroCheck.IsEnabled = probeOnlyCheck.IsChecked != true;
                UpdateActionButton();
            };
            probeOnlyCheck.Checked += (_, __) =>
            {
                zeroCheck.IsEnabled = false;
                requireBaCheck.IsEnabled = false;
                UpdateActionButton();
            };
            probeOnlyCheck.Unchecked += (_, __) =>
            {
                zeroCheck.IsEnabled = true;
                requireBaCheck.IsEnabled = SelectedMethod() == "paypal";
                UpdateActionButton();
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
            LoadMethodProxyConfiguration(loadCountries: true);

            saveProxyBtn.Click += (_, __) =>
            {
                SettingsSaveResult saved = SaveMethodProxyConfiguration();
                resultBox.Text = saved.Ok
                    ? "[成功] 已保存当前支付方式的 Checkout / Approve-Update 代理池。"
                    : "[失败] " + saved.Error;
            };

            testProxyBtn.Click += async (_, __) =>
            {
                SaveSelection();
                var args = ProtocolPaymentExecutionPlanner.CreateProxyTestArguments(
                    SelectedMethod(),
                    "",
                    checkoutProxyPoolBox.Text,
                    approveProxyPoolBox.Text,
                    ComboCode(checkoutCountryCombo),
                    ComboCode(approveCountryCombo),
                    ComboCode(updateCountryCombo)).ToList();

                resultBox.Text = "正在测试 checkout / approve / update 代理出口...";
                testProxyBtn.IsEnabled = false;
                extractBtn.IsEnabled = false;
                try
                {
                    string rawResult = await RunBackendWithResultAsync("测试协议支付代理", args);
                    ProxyTestResult proxyResult = BackendResultInterpreter.ParseProxyTestResult(rawResult);
                    var lines = new List<string>
                    {
                        proxyResult.AllOk ? "[成功] 代理出口符合选择" : "[失败] 存在不可用或地区不匹配的代理"
                    };
                    foreach (ProxyTestStageResult stage in proxyResult.Stages)
                    {
                        string detail = $"{stage.Stage}: {stage.Ip} / {stage.ActualCountry} (目标 {stage.ExpectedCountry})";
                        if (stage.Error.Length > 0) detail += $" - {stage.Error}";
                        lines.Add(detail);
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
                if (probeOnlyCheck.IsChecked != true
                    && method == "blik"
                    && (blikCodeBox.Text.Trim().Length != 6 || !blikCodeBox.Text.Trim().All(char.IsDigit)))
                {
                    resultBox.Text = "请输入有效的 6 位 BLIK Code";
                    return;
                }
                string country = "US";
                if (countryCombo.SelectedItem is ComboBoxItem ci && ci.Content.ToString().Length >= 2)
                    country = ci.Content.ToString().Substring(0, 2);

                bool requireZero = zeroCheck.IsChecked == true;
                bool requireBaToken = requireBaCheck.IsChecked == true;
                SaveSelection();

                extractBtn.IsEnabled = false;
                testProxyBtn.IsEnabled = false;
                cancelBtn.IsEnabled = true;
                copyBtn.IsEnabled = false;
                openQrBtn.IsEnabled = false;
                string transientSessionFile = "";
                using var cancellation = new CancellationTokenSource();
                executionCancellation = cancellation;
                ProtocolPaymentExecutionPlan plan = null;

                try
                {
                    string sessionFile;
                    if (selectedAccount == null)
                    {
                        transientSessionFile = Path.Combine(Path.GetTempPath(), "protocol_payment_at_" + Guid.NewGuid().ToString("N") + ".json");
                        File.WriteAllText(
                            transientSessionFile,
                            JsonSerializer.Serialize(new Dictionary<string, string> { ["access_token"] = at }),
                            new UTF8Encoding(false));
                        sessionFile = transientSessionFile;
                    }
                    else
                    {
                        sessionFile = SessionFileFor(selectedAccount);
                    }

                    plan = ProtocolPaymentExecutionPlanner.Create(
                        new ProtocolPaymentExecutionRequest(
                            method,
                            country,
                            "",
                            checkoutProxyPoolBox.Text,
                            approveProxyPoolBox.Text,
                            jitRefreshCheck.IsChecked == true,
                            probeOnlyCheck.IsChecked == true,
                            requireZero,
                            requireBaToken,
                            blikCodeBox.Text,
                            ComboCode(checkoutCountryCombo),
                            ComboCode(approveCountryCombo),
                            ComboCode(updateCountryCombo),
                            selectedAccount?.Identifier ?? "",
                            sessionFile));
                    var args = plan.Arguments.ToList();
                    resultBox.Text = plan.StatusText;
                    int timeoutMs = ProtocolPaymentBackendTimeoutMs(method);
                    Log("启动：python " + FormatBackendArgsForDisplay(args));
                    BackendCommandResult backendResult = await backendClient.RunAsync(
                        BackendCommand.Create(plan.TaskName, args, timeoutMs),
                        cancellationToken: cancellation.Token);

                    // Use BackendResultInterpreter for timeout detection
                    BackendExecutionResult execution = BackendResultInterpreter.Interpret(
                        backendResult, plan.TaskName, timeoutMs / 1000);

                    if (!execution.IsSuccess || execution.State != "completed")
                    {
                        ProtocolPaymentResultPresentation failedPresentation = execution.State switch
                        {
                            "timed_out" => ProtocolPaymentResultPresenter.Aborted(plan, "timed_out"),
                            "cancelled" => ProtocolPaymentResultPresenter.Aborted(plan, "cancelled"),
                            _ when execution.Payload.HasValue => ProtocolPaymentResultPresenter.Parse(
                                execution.Payload.Value.GetRawText()),
                            _ => ProtocolPaymentResultPresenter.Parse(execution.DisplayText)
                        };
                        resultBox.Text = failedPresentation.Text;
                        lastUrl = failedPresentation.Url;
                        lastQrPath = failedPresentation.QrPath;
                        copyBtn.IsEnabled = lastUrl.Length > 0;
                        openQrBtn.IsEnabled = lastQrPath.Length > 0 && File.Exists(lastQrPath);
                        return;
                    }

                    string result = execution.Payload.HasValue
                        ? execution.Payload.Value.GetRawText()
                        : execution.DisplayText;

                    ProtocolPaymentResultPresentation presentation = ProtocolPaymentResultPresenter.Parse(result);
                    resultBox.Text = presentation.Text;
                    lastUrl = presentation.Url;
                    lastQrPath = presentation.QrPath;
                    copyBtn.IsEnabled = lastUrl.Length > 0;
                    openQrBtn.IsEnabled = lastQrPath.Length > 0 && File.Exists(lastQrPath);
                }
                catch (OperationCanceledException)
                {
                    ProtocolPaymentResultPresentation cancelled = ProtocolPaymentResultPresenter.Aborted(plan, "cancelled");
                    resultBox.Text = cancelled.Text;
                    lastUrl = cancelled.Url;
                    lastQrPath = cancelled.QrPath;
                    copyBtn.IsEnabled = false;
                    openQrBtn.IsEnabled = false;
                }
                catch (TimeoutException)
                {
                    ProtocolPaymentResultPresentation timedOut = ProtocolPaymentResultPresenter.Aborted(plan, "timed_out");
                    resultBox.Text = timedOut.Text;
                    lastUrl = timedOut.Url;
                    lastQrPath = timedOut.QrPath;
                    copyBtn.IsEnabled = false;
                    openQrBtn.IsEnabled = false;
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
                    if (ReferenceEquals(executionCancellation, cancellation))
                        executionCancellation = null;
                    extractBtn.IsEnabled = true;
                    testProxyBtn.IsEnabled = true;
                    cancelBtn.IsEnabled = false;
                    if (closeAfterCancellation)
                        win.Close();
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

            cancelBtn.Click += (_, __) =>
            {
                if (executionCancellation == null) return;
                resultBox.Text = "正在取消协议支付任务...";
                cancelBtn.IsEnabled = false;
                executionCancellation.Cancel();
            };

            closeBtn.Click += (_, __) =>
            {
                SaveSelection();
                win.Close();
            };
            win.Closing += (_, args) =>
            {
                if (executionCancellation == null) return;
                args.Cancel = true;
                closeAfterCancellation = true;
                resultBox.Text = "正在取消协议支付任务...";
                cancelBtn.IsEnabled = false;
                executionCancellation.Cancel();
            };
            win.Closed += (_, __) => SaveSelection();

            win.ShowDialog();
        }

        // Title plus the optional selected-account banner. Extracted from
        // ShowProtocolPaymentDialog: it only appends to the panel and captures
        // nothing the dialog's event handlers depend on.
        private void AddProtocolDialogHeader(StackPanel mainPanel, PoolRow selectedAccount)
        {
            mainPanel.Children.Add(new TextBlock
            {
                Text = "协议支付",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 16),
            });

            if (selectedAccount == null) return;
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

        // Shared factory for the dialog's action buttons. The caller keeps each
        // returned instance in its own local, so event wiring and closures are
        // unchanged; only the repeated styling moves here. ``primary`` keeps the
        // default accent button chrome; others take the flat panel styling.
        private Button BuildProtocolDialogButton(
            string content,
            double minWidth,
            bool primary = false,
            bool enabled = true,
            double rightMargin = 8)
        {
            var button = new Button
            {
                Content = content,
                Height = 32,
                MinWidth = minWidth,
                IsEnabled = enabled,
                Margin = new Thickness(0, 0, rightMargin, 0),
            };
            if (primary)
            {
                button.FontWeight = FontWeights.SemiBold;
            }
            else
            {
                button.Background = (System.Windows.Media.Brush)FindResource("PanelBg");
                button.Foreground = (System.Windows.Media.Brush)FindResource("TextMain");
                button.BorderBrush = (System.Windows.Media.Brush)FindResource("Line");
            }
            return button;
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
            defaults.CheckoutCountry = FirstNonEmpty(settingsService.GetString("paypal.stage_proxy_countries.checkout"), "US");
            defaults.ApproveCountry = FirstNonEmpty(settingsService.GetString("paypal.stage_proxy_countries.approve"), "TR");
            defaults.UpdateCountry = FirstNonEmpty(settingsService.GetString("paypal.stage_proxy_countries.promotion"), "TR");
            defaults.TargetCountry = FirstNonEmpty(settingsService.GetString("paypal.target_country"), "US");
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
            if (int.TryParse(settingsService.GetString("protocol_payments.timeout_seconds"), out int configured))
                seconds = configured;
            string methodPath = "protocol_payments.methods." + NormalizePaymentMethod(paymentMethod) + ".timeout_seconds";
            if (int.TryParse(settingsService.GetString(methodPath), out int methodConfigured))
                seconds = methodConfigured;
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
