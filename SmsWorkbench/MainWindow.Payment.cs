namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Payment link and AT BA-link actions
        private void OpenSessions_Click(object sender, RoutedEventArgs e) => OpenPath(GetSessionsDir());

        private void OpenDatabase_Click(object sender, RoutedEventArgs e) => OpenPath(GetDatabasePath());

        private void OpenMailboxPool_Click(object sender, RoutedEventArgs e) => OpenPath(GetMailboxTokenFile());

        private void OpenPayPalLink_Click(object sender, RoutedEventArgs e)
        {
            PoolRow row = SelectedAccountRow();
            if (row == null) return;
            if (string.IsNullOrWhiteSpace(row.PayPalUrl))
            {
                MessageBox.Show("选中账号没有可打开的 PayPal 支付链接。", "无支付链接", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            OpenPayPalUrl(row.PayPalUrl, row.Identifier);
        }

        private void RegeneratePayPalLink_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedRowsOrCurrent()
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog("未选择账号", "请先勾选或选择要重新生成链接的账号记录。");
                return;
            }
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
            MarkPayPalComplete(SelectedRowsOrCurrent());
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
                MessageBox.Show("请先勾选或选择账号记录。", "未选择账号", MessageBoxButton.OK, MessageBoxImage.Information);
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
            ShowAtPaymentDialog();
        }

        /// <summary>
        /// AT Payment dialog supporting both PayPal BA link and UPI QR extraction.
        /// UPI mode uses the full 7-stage pipeline: checkout → init → free trial detection →
        /// tax region → stripe confirm → chatgpt approve → poll for upi:// URI → QR.
        /// </summary>
        private void ShowAtPaymentDialog()
        {
            var win = new Window
            {
                Title = "AT 支付 (PayPal BA / UPI)",
                Width = 620,
                Height = 720,
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
                Text = "AT 支付 — PayPal BA / UPI QR",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 16),
            });

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
            methodCombo.Items.Add(new ComboBoxItem { Content = "UPI — 印度统一支付接口 (upi:// QR)" });
            methodCombo.Items.Add(new ComboBoxItem { Content = "PayPal — BA 授权链接 (hosted)" });
            mainPanel.Children.Add(methodCombo);

            // ── AT 输入 ───────────────────────────────────────────────────
            mainPanel.Children.Add(new TextBlock
            {
                Text = "Access Token (JWT)",
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 4),
            });
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
                "IN - 印度 (UPI)", "DE - 德国", "GB - 英国", "US - 美国",
                "AU - 澳大利亚", "JP - 日本", "FR - 法国", "BR - 巴西",
                "SG - 新加坡", "CA - 加拿大", "NZ - 新西兰", "IE - 爱尔兰",
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
                Height = 28,
                FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                FontSize = 12,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                Margin = new Thickness(0, 0, 0, 4),
            };
            mainPanel.Children.Add(proxyBox);

            var stageProxyPanel = new StackPanel { Margin = new Thickness(0, 0, 0, 12) };
            stageProxyPanel.Children.Add(new TextBlock
            {
                Text = "分段代理 (格式: checkout=... provider=... approve=...)",
                FontSize = 11,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 2),
            });
            var stageProxyBox = new TextBox
            {
                Height = 28,
                FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                FontSize = 11,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
            };
            stageProxyPanel.Children.Add(stageProxyBox);
            mainPanel.Children.Add(stageProxyPanel);

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
                Content = "必须返回 PayPal BA 授权 URL (仅 PayPal 模式)",
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
            btnPanel.Children.Add(extractBtn);
            btnPanel.Children.Add(copyBtn);
            btnPanel.Children.Add(openQrBtn);
            btnPanel.Children.Add(closeBtn);
            mainPanel.Children.Add(btnPanel);

            scrollViewer.Content = mainPanel;
            win.Content = scrollViewer;

            string lastUrl = "";
            string lastQrPath = "";

            // ── 支付方式切换时更新国家默认值 ──────────────────────────────
            methodCombo.SelectionChanged += (_, __) =>
            {
                bool isUpi = methodCombo.SelectedIndex == 0;
                if (isUpi)
                    countryCombo.SelectedIndex = 0; // IN
                else
                    countryCombo.SelectedIndex = 2; // GB
                requireBaCheck.IsEnabled = !isUpi;
            };

            // ── 提取按钮 ──────────────────────────────────────────────────
            extractBtn.Click += async (_, __) =>
            {
                string at = atBox.Text.Trim();
                if (string.IsNullOrEmpty(at))
                {
                    resultBox.Text = "请输入 Access Token";
                    return;
                }

                bool isUpi = methodCombo.SelectedIndex == 0;
                string country = "IN";
                if (countryCombo.SelectedItem is ComboBoxItem ci && ci.Content.ToString().Length >= 2)
                    country = ci.Content.ToString().Substring(0, 2);

                string proxy = proxyBox.Text.Trim();
                string stageProxies = stageProxyBox.Text.Trim();
                bool requireZero = zeroCheck.IsChecked == true;
                bool requireBaToken = requireBaCheck.IsChecked == true;

                resultBox.Text = isUpi ? "正在提取 UPI QR (7 阶段流程)..." : "正在提取 PayPal BA 链接...";
                extractBtn.IsEnabled = false;
                copyBtn.IsEnabled = false;
                openQrBtn.IsEnabled = false;

                try
                {
                    var args = new List<string>();

                    if (isUpi)
                    {
                        args.AddRange(new[] { "--generate-upi-qr", "--at", at, "--target-country", country });
                    }
                    else
                    {
                        args.AddRange(new[] { "--generate-ba-link", "--at", at, "--target-country", country });
                    }

                    if (!string.IsNullOrEmpty(proxy))
                        args.AddRange(new[] { "--proxy", proxy });

                    if (!string.IsNullOrEmpty(stageProxies))
                    {
                        var parts = stageProxies.Split(new[] { ' ', ';', ',' }, StringSplitOptions.RemoveEmptyEntries);
                        foreach (var part in parts)
                        {
                            var kv = part.Split('=', 2);
                            if (kv.Length == 2)
                            {
                                string key = kv[0].Trim().ToLowerInvariant();
                                string val = kv[1].Trim();
                                if (key == "checkout" || key == "checkout-proxy")
                                    args.AddRange(new[] { "--checkout-proxy", val });
                                else if (key == "provider" || key == "provider-proxy")
                                    args.AddRange(new[] { "--provider-proxy", val });
                                else if (key == "approve" || key == "approve-proxy")
                                    args.AddRange(new[] { "--approve-proxy", val });
                            }
                        }
                    }

                    if (!requireZero)
                        args.Add("--no-require-zero");
                    if (!isUpi && requireBaToken)
                        args.Add("--require-ba-token");

                    string taskName = isUpi ? "AT UPI QR 提取" : "AT BA 链接提取";
                    var result = await Task.Run(() => RunBackendWithResult(taskName, args));

                    // 解析 JSON 结果
                    try
                    {
                        var json = System.Text.Json.JsonDocument.Parse(result);
                        var root = json.RootElement;
                        if (root.TryGetProperty("ok", out var ok) && ok.GetBoolean())
                        {
                            var sb = new StringBuilder();
                            sb.AppendLine("[成功] 提取成功!");
                            sb.AppendLine();

                            // URL / UPI URI
                            string url = "";
                            if (root.TryGetProperty("upi_uri", out var upiUri) && !string.IsNullOrEmpty(upiUri.GetString()))
                            {
                                url = upiUri.GetString() ?? "";
                                sb.AppendLine($"UPI URI: {url}"); // UPI URI 为技术字段名，保留
                            }
                            else if (root.TryGetProperty("url", out var urlEl))
                            {
                                url = urlEl.GetString() ?? "";
                                sb.AppendLine($"链接: {url}");
                            }

                            if (root.TryGetProperty("hosted_url", out var hostedEl))
                                sb.AppendLine($"托管 URL: {hostedEl.GetString()}");

                            if (root.TryGetProperty("link_type", out var ltEl))
                                sb.AppendLine($"链接类型: {ltEl.GetString()}");

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

            closeBtn.Click += (_, __) => win.Close();

            win.ShowDialog();
        }

    }
}
