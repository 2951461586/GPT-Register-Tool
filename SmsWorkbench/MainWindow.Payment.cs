using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Globalization;
using System.Windows.Data;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using FluentWindow = Wpf.Ui.Controls.FluentWindow;

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
            ShowAtExtractBaLinkDialog();
        }

        private void ShowAtExtractBaLinkDialog()
        {
            var win = new Window
            {
                Title = "AT 提取 BA 链接",
                Width = 560,
                Height = 620,
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

            // 标题
            mainPanel.Children.Add(new TextBlock
            {
                Text = "输入 Access Token 提取 PayPal BA 链接",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 16),
            });

            // AT 输入
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

            // 目标国家
            mainPanel.Children.Add(new TextBlock
            {
                Text = "目标国家",
                FontSize = 13,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 4),
            });
            var countryCombo = new ComboBox
            {
                Height = 32,
                SelectedIndex = 1,
                Background = (System.Windows.Media.Brush)FindResource("PanelBg"),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                BorderBrush = (System.Windows.Media.Brush)FindResource("Line"),
                Margin = new Thickness(0, 0, 0, 12),
            };
            foreach (var c in new[] { "DE - Germany", "GB - United Kingdom", "US - United States", "AU - Australia", "JP - Japan", "FR - France", "IN - India", "BR - Brazil" })
                countryCombo.Items.Add(new ComboBoxItem { Content = c });
            mainPanel.Children.Add(countryCombo);

            // 代理配置
            mainPanel.Children.Add(new TextBlock
            {
                Text = "代理配置 (可选，留空使用配置文件)",
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

            // 选项
            var optionPanel = new StackPanel { Orientation = Orientation.Vertical, Margin = new Thickness(0, 0, 0, 16) };
            var zeroCheck = new CheckBox
            {
                Content = "严格要求 0 元金额 / Strict zero due",
                IsChecked = true,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 6),
            };
            var requireBaCheck = new CheckBox
            {
                Content = "必须返回 PayPal BA approve URL / Require BA approve URL",
                IsChecked = true,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 0),
            };
            optionPanel.Children.Add(zeroCheck);
            optionPanel.Children.Add(requireBaCheck);
            mainPanel.Children.Add(optionPanel);

            // 结果区域
            var resultBox = new TextBox
            {
                Height = 100,
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

            // 按钮面板
            var btnPanel = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            var extractBtn = new Button
            {
                Content = "提取 BA 链接",
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
            btnPanel.Children.Add(closeBtn);
            mainPanel.Children.Add(btnPanel);

            scrollViewer.Content = mainPanel;
            win.Content = scrollViewer;

            string lastUrl = "";

            extractBtn.Click += async (_, __) =>
            {
                string at = atBox.Text.Trim();
                if (string.IsNullOrEmpty(at))
                {
                    resultBox.Text = "请输入 Access Token";
                    return;
                }

                string country = "GB";
                if (countryCombo.SelectedItem is ComboBoxItem ci && ci.Content.ToString().Length >= 2)
                    country = ci.Content.ToString().Substring(0, 2);

                string proxy = proxyBox.Text.Trim();
                string stageProxies = stageProxyBox.Text.Trim();
                bool requireZero = zeroCheck.IsChecked == true;
                bool requireBaToken = requireBaCheck.IsChecked == true;

                resultBox.Text = "正在提取...";
                extractBtn.IsEnabled = false;
                copyBtn.IsEnabled = false;

                try
                {
                    var args = new List<string>
                    {
                        "--generate-ba-link",
                        "--at", at,
                        "--target-country", country,
                    };

                    if (!string.IsNullOrEmpty(proxy))
                        args.AddRange(new[] { "--proxy", proxy });

                    if (!string.IsNullOrEmpty(stageProxies))
                    {
                        // 解析 checkout=... provider=... approve=...
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
                    if (requireBaToken)
                        args.Add("--require-ba-token");

                    var result = await Task.Run(() => RunBackendWithResult("AT 提取 BA 链接", args));
                    resultBox.Text = result;

                    // 解析 JSON 结果
                    try
                    {
                        var json = System.Text.Json.JsonDocument.Parse(result);
                        var root = json.RootElement;
                        if (root.TryGetProperty("ok", out var ok) && ok.GetBoolean())
                        {
                            if (root.TryGetProperty("url", out var url))
                            {
                                lastUrl = url.GetString() ?? "";
                                copyBtn.IsEnabled = !string.IsNullOrEmpty(lastUrl);
                                resultBox.Text = $"✅ 提取成功!\n\nURL: {lastUrl}\n\n" +
                                    (root.TryGetProperty("ba_token", out var bt) ? $"BA Token: {bt.GetString()}\n" : "") +
                                    (root.TryGetProperty("amount", out var amt) ? $"金额: {amt}" : "") +
                                    (root.TryGetProperty("currency", out var cur) ? $" {cur.GetString()}\n" : "") +
                                    (root.TryGetProperty("target_country", out var tc) ? $"目标国: {tc.GetString()}" : "");
                            }
                        }
                        else
                        {
                            if (root.TryGetProperty("error", out var err))
                                resultBox.Text = $"❌ 失败: {err.GetString()}";
                        }
                    }
                    catch
                    {
                        // 非 JSON 结果，直接显示
                    }
                }
                catch (Exception ex)
                {
                    resultBox.Text = $"❌ 异常: {ex.Message}";
                }
                finally
                {
                    extractBtn.IsEnabled = true;
                }
            };

            copyBtn.Click += (_, __) =>
            {
                if (!string.IsNullOrEmpty(lastUrl))
                {
                    System.Windows.Clipboard.SetText(lastUrl);
                    copyBtn.Content = "已复制!";
                    Task.Delay(1500).ContinueWith(_ => Dispatcher.Invoke(() => copyBtn.Content = "复制链接"));
                }
            };

            closeBtn.Click += (_, __) => win.Close();

            win.ShowDialog();
        }

    }
}
