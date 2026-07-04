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
        // Registration, SMS, K12 and selection mailbox argument builders
        private void RegisterFromPool_Click(object sender, RoutedEventArgs e)
        {
            var args = new List<string> { "--count", CountValue().ToString(), "--workers", "4" };
            AddProxy(args);
            AddPaypalOption(args);
            RunBackend("邮箱池注册", args);
        }

        private void ImportChataiMailbox_Click(object sender, RoutedEventArgs e)
        {
            var dialog = new Microsoft.Win32.OpenFileDialog
            {
                Filter = "文本文件 (*.txt)|*.txt|所有文件 (*.*)|*.*",
                Title = "选择 Chatai 邮箱文件"
            };
            if (dialog.ShowDialog() != true) return;

            string path = dialog.FileName;
            string[] lines;
            try
            {
                lines = File.ReadAllLines(path, Encoding.UTF8);
            }
            catch (Exception ex)
            {
                MessageBox.Show("读取文件失败：" + ex.Message, "错误", MessageBoxButton.OK, MessageBoxImage.Error);
                return;
            }

            int imported = 0, skipped = 0;
            var targetFile = Path.Combine(rootDir, "hotmail.txt");
            var existingLines = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (File.Exists(targetFile))
            {
                foreach (string existing in File.ReadAllLines(targetFile, Encoding.UTF8))
                {
                    string trimmed = existing.Trim();
                    if (trimmed.Length > 0) existingLines.Add(trimmed);
                }
            }

            var newLines = new List<string>();
            foreach (string raw in lines)
            {
                string line = raw.Trim();
                if (line.Length == 0 || line.StartsWith("#")) continue;
                if (!line.Contains("----")) { skipped++; continue; }
                string[] parts = line.Split(new[] { "----" }, StringSplitOptions.None);
                if (parts.Length < 4) { skipped++; continue; }
                if (existingLines.Contains(line)) { skipped++; continue; }
                newLines.Add(line);
                imported++;
            }

            if (newLines.Count > 0)
            {
                File.AppendAllLines(targetFile, newLines, Encoding.UTF8);
            }

            ChataiMailboxFilePath = targetFile;
            RefreshPools();
            MessageBox.Show($"导入完成：成功 {imported} 条，跳过 {skipped} 条。", "导入结果", MessageBoxButton.OK, MessageBoxImage.Information);
        }

        private void ViewInbox_Click(object sender, RoutedEventArgs e)
        {
            PoolRow row = SelectedRow ?? (AccountGrid.SelectedItem as PoolRow);
            if (row == null)
            {
                MessageBox.Show("请先选择一条 Chatai 或 edu.liziai.cloud 邮箱记录。", "未选择邮箱", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            string mailboxLine = FindMailboxLineForRow(row);
            if (!IsCfWorkerRow(row) && (string.IsNullOrWhiteSpace(row.RawRefreshToken) || string.IsNullOrWhiteSpace(row.ClientId)))
            {
                MessageBox.Show("选中记录不是 Chatai 或 edu.liziai.cloud 邮箱。", "格式不匹配", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            ShowInboxDialog(row);
        }

        private void OneClickRegister_Click(object sender, RoutedEventArgs e)
        {
            if (TryCreateSelectedUnregisteredMailboxFile(out string pendingMailboxArg, out string pendingMailboxFile, out int pendingSelectedCount, out int pendingRowCount))
            {
                RegisterOptions selectedOptions = ShowSelectedRegisterOptionsDialog(pendingSelectedCount);
                if (selectedOptions == null) return;
                var pendingArgs = new List<string> { pendingMailboxArg, pendingMailboxFile, "--count", pendingSelectedCount.ToString(), "--workers", selectedOptions.Workers.ToString() };
                AddRegistrationAtOnlyArgs(pendingArgs);
                AddProxy(pendingArgs);
                AddPaypalOption(pendingArgs, selectedOptions.PaymentMethod, selectedOptions.SkipPaymentLink);
                RunBackend(selectedOptions.SkipPaymentLink ? "选中未注册邮箱注册" : "选中未注册邮箱注册+支付链接", pendingArgs);
                return;
            }
            if (pendingRowCount > 0)
            {
                ShowThemedInfoDialog("邮箱记录不完整", "选中的未注册邮箱缺少可用邮箱原始记录，无法直接注册。");
                return;
            }

            if (TryCreateSelectedMailboxFile(out string selectedArg, out string selectedFile, out int selectedCount))
            {
                RegisterOptions selectedOptions = ShowSelectedRegisterOptionsDialog(selectedCount);
                if (selectedOptions == null) return;
                var selectedArgs = new List<string> { selectedArg, selectedFile, "--count", selectedCount.ToString(), "--workers", selectedOptions.Workers.ToString() };
                AddRegistrationAtOnlyArgs(selectedArgs);
                AddProxy(selectedArgs);
                AddPaypalOption(selectedArgs, selectedOptions.PaymentMethod, selectedOptions.SkipPaymentLink);
                RunBackend(selectedOptions.SkipPaymentLink ? "选中邮箱注册" : "选中邮箱注册+支付链接", selectedArgs);
                return;
            }

            RegisterOptions options = ShowRegisterOptionsDialog();
            if (options == null) return;

            if (options.Source == "phone")
            {
                var phoneArgs = new List<string>
                {
                    "--phone-register",
                    "--count",
                    options.Count.ToString(),
                };
                if (!string.IsNullOrWhiteSpace(ProxyText)) phoneArgs.AddRange(new[] { "--proxy", ProxyText.Trim() });
                AddPaypalOption(phoneArgs, options.PaymentMethod, options.SkipPaymentLink);
                RunBackend(options.SkipPaymentLink ? "手机号注册 (SMSBower)" : "手机号注册+支付链接 (SMSBower)", phoneArgs);
                return;
            }

            if (options.Source == "cfworker")
            {
                var cfArgs = new List<string>
                {
                    "--buy-cfworker-mailbox",
                    "--cfworker-domain",
                    GetConfiguredCfWorkerDomain(),
                    "--count",
                    options.Count.ToString(),
                    "--workers",
                    options.Workers.ToString()
                };
                AddRegistrationAtOnlyArgs(cfArgs);
                AddProxy(cfArgs);
                AddPaypalOption(cfArgs, options.PaymentMethod, options.SkipPaymentLink);
                RunBackend(options.SkipPaymentLink ? "CFWorker邮箱注册" : "CFWorker邮箱注册+支付链接", cfArgs);
                return;
            }

            string mailboxArg = "--chatai-mailbox-file";
            string mailboxFile = GetChataiMailboxFilePath();
            int count = options.Count;
            string taskName = options.SkipPaymentLink ? "一键注册" : "一键注册+支付链接";
            if (string.IsNullOrWhiteSpace(mailboxFile) || !File.Exists(mailboxFile))
            {
                ShowThemedInfoDialog("缺少邮箱文件", "未选择邮箱，且未找到 Chatai 邮箱文件。请先导入邮箱，或勾选要注册的邮箱记录。");
                return;
            }
            var args = new List<string> { mailboxArg, mailboxFile, "--count", count.ToString(), "--workers", options.Workers.ToString() };
            AddRegistrationAtOnlyArgs(args);
            AddProxy(args);
            AddPaypalOption(args, options.PaymentMethod, options.SkipPaymentLink);
            RunBackend(taskName, args);
        }

        private void AddRegistrationAtOnlyArgs(List<string> args)
        {
            args.Add("--registration-at-only");
            args.Add("--no-phone-reuse");
        }

        private void OneClickSms_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedRowsOrCurrent()
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            if (rows.Count == 0)
            {
                MessageBox.Show("请先勾选或选择要接码的邮箱账号。", "未选择账号", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            var args = new List<string> { "--one-click-sms", "--workers", "1", "--refresh-timeout", "60" };
            if (rows.Count > 1)
            {
                string emailFile = Path.Combine(Path.GetTempPath(), "oneclick_sms_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
                File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
                args.AddRange(new[] { "--email-file", emailFile });
            }
            else
            {
                args.AddRange(new[] { "--email", rows[0].Identifier });
                AddSessionFileArg(args, rows[0]);
            }
            AddProxy(args);
            RunBackend("一键接码(" + rows.Count + ")", args);
        }

        private void OneClickScan_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedRowsOrCurrent()
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .ToList();
            if (rows.Count == 0)
            {
                rows = allRows
                    .Where(FilterRow)
                    .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                    .ToList();
            }
            rows = rows
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog("一键扫号", "没有找到可扫描的账号。请先勾选账号，或切换到包含账号的筛选范围。");
                return;
            }

            var args = new List<string> { "--one-click-scan", "--workers", Math.Min(8, Math.Max(1, rows.Count)).ToString(), "--refresh-timeout", "90" };
            if (rows.Count > 1)
            {
                string emailFile = Path.Combine(Path.GetTempPath(), "oneclick_scan_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
                File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
                args.AddRange(new[] { "--email-file", emailFile });
            }
            else
            {
                args.AddRange(new[] { "--email", rows[0].Identifier });
                AddSessionFileArg(args, rows[0]);
            }
            AddProxy(args);
            RunBackend("一键扫号(" + rows.Count + ")", args);
        }

        private void OneClickK12_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedRowsOrCurrent()
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .Where(IsRegisteredRow)
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            if (rows.Count == 0)
            {
                rows = allRows
                    .Where(FilterRow)
                    .Where(IsRegisteredRow)
                    .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                    .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                    .Select(g => g.First())
                    .ToList();
            }
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog("一键K12", "没有找到可进入 K12 workspace 的已注册账号。请先选择带 access_token 的账号。");
                return;
            }

            K12Options options = ShowK12OptionsDialog(rows.Count);
            if (options == null) return;

            var args = new List<string>
            {
                "--one-click-k12",
                "--k12-route",
                options.Route,
            };
            if (!string.IsNullOrWhiteSpace(options.WorkspaceIds))
            {
                args.Add("--k12-workspace-ids");
                args.Add(options.WorkspaceIds);
            }
            args.AddRange(new[]
            {
                "--workers",
                options.Workers.ToString(),
                "--k12-retries",
                options.Retries.ToString(),
                "--k12-retry-backoff",
                options.RetryBackoffSeconds.ToString(CultureInfo.InvariantCulture),
                "--k12-invite-timeout",
                options.InviteTimeoutSeconds.ToString(),
                "--refresh-timeout",
                "30"
            });
            if (options.AutoAcceptInvite)
            {
                args.Add("--k12-auto-accept");
            }
            if (rows.Count > 1)
            {
                string emailFile = Path.Combine(Path.GetTempPath(), "oneclick_k12_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
                File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
                args.AddRange(new[] { "--email-file", emailFile });
            }
            else
            {
                args.AddRange(new[] { "--email", rows[0].Identifier });
                AddSessionFileArg(args, rows[0]);
            }
            AddProxy(args);
            RunBackend("一键K12(" + rows.Count + ")", args);
        }

        private K12Options ShowK12OptionsDialog(int accountCount)
        {
            var dialog = new Window
            {
                Title = "一键K12",
                Owner = this,
                Width = 640,
                Height = 430,
                MinWidth = 600,
                MinHeight = 400,
                ResizeMode = ResizeMode.CanResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(140) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            var hint = new TextBlock
            {
                Text = "将 " + Math.Max(1, accountCount).ToString() + " 个账号批量执行 K12 workspace 请求/接受/退出。",
                Margin = new Thickness(0, 0, 0, 10),
                TextWrapping = TextWrapping.Wrap,
                LineHeight = 20,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub")
            };
            Grid.SetRow(hint, 0);
            Grid.SetColumnSpan(hint, 2);
            root.Children.Add(hint);

            var wsLabel = new TextBlock { Text = "Workspace ID", VerticalAlignment = VerticalAlignment.Top, Margin = new Thickness(0, 4, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var wsBox = new TextBox
            {
                Text = "631e1603-06cf-4f0b-b79b-d09fbfcfe98d",
                AcceptsReturn = true,
                TextWrapping = TextWrapping.Wrap,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                Margin = new Thickness(0, 0, 0, 10),
                MinHeight = 92
            };
            Grid.SetRow(wsLabel, 1);
            Grid.SetColumn(wsLabel, 0);
            Grid.SetRow(wsBox, 1);
            Grid.SetColumn(wsBox, 1);
            root.Children.Add(wsLabel);
            root.Children.Add(wsBox);

            var routeLabel = new TextBlock { Text = "动作", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var routeBox = new ComboBox { Margin = new Thickness(0, 0, 0, 10) };
            routeBox.Items.Add(new ComboBoxItem { Content = "Request - 主动申请加入", Tag = "request" });
            routeBox.Items.Add(new ComboBoxItem { Content = "Accept - 接受已有邀请", Tag = "accept" });
            routeBox.Items.Add(new ComboBoxItem { Content = "Leave - 退出工作空间", Tag = "leave" });
            routeBox.SelectedIndex = 0;
            Grid.SetRow(routeLabel, 2);
            Grid.SetColumn(routeLabel, 0);
            Grid.SetRow(routeBox, 2);
            Grid.SetColumn(routeBox, 1);
            root.Children.Add(routeLabel);
            root.Children.Add(routeBox);

            var retryLabel = new TextBlock { Text = "重试/等待", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var retryPanel = new WrapPanel { Margin = new Thickness(0, 0, 0, 10), VerticalAlignment = VerticalAlignment.Center };
            var retryBox = new TextBox { Text = "2", Width = 44, Margin = new Thickness(0, 0, 8, 0) };
            var backoffBox = new TextBox { Text = "5", Width = 44, Margin = new Thickness(0, 0, 8, 0) };
            var inviteTimeoutBox = new TextBox { Text = "240", Width = 56, Margin = new Thickness(0, 0, 8, 0) };
            retryPanel.Children.Add(new TextBlock { Text = "次数", Margin = new Thickness(0, 2, 6, 0), VerticalAlignment = VerticalAlignment.Center, Foreground = (System.Windows.Media.Brush)FindResource("TextSub") });
            retryPanel.Children.Add(retryBox);
            retryPanel.Children.Add(new TextBlock { Text = "间隔秒", Margin = new Thickness(0, 2, 6, 0), VerticalAlignment = VerticalAlignment.Center, Foreground = (System.Windows.Media.Brush)FindResource("TextSub") });
            retryPanel.Children.Add(backoffBox);
            retryPanel.Children.Add(new TextBlock { Text = "邀请秒", Margin = new Thickness(0, 2, 6, 0), VerticalAlignment = VerticalAlignment.Center, Foreground = (System.Windows.Media.Brush)FindResource("TextSub") });
            retryPanel.Children.Add(inviteTimeoutBox);
            Grid.SetRow(retryLabel, 3);
            Grid.SetColumn(retryLabel, 0);
            Grid.SetRow(retryPanel, 3);
            Grid.SetColumn(retryPanel, 1);
            root.Children.Add(retryLabel);
            root.Children.Add(retryPanel);

            var autoAcceptBox = new CheckBox
            {
                Content = new TextBlock
                {
                    Text = "请求成功后自动收 k12-invite 邮件并接受/进入空间",
                    TextWrapping = TextWrapping.Wrap,
                    LineHeight = 20,
                    MaxWidth = 440
                },
                IsChecked = true,
                Margin = new Thickness(0, 0, 0, 10),
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub")
            };
            Grid.SetRow(autoAcceptBox, 4);
            Grid.SetColumn(autoAcceptBox, 1);
            root.Children.Add(autoAcceptBox);
            string defaultWorkspaceId = "631e1603-06cf-4f0b-b79b-d09fbfcfe98d";
            routeBox.SelectionChanged += (_, __) =>
            {
                string selectedRoute = ((routeBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "request";
                bool canAutoAccept = selectedRoute == "request";
                autoAcceptBox.IsEnabled = canAutoAccept;
                if (!canAutoAccept) autoAcceptBox.IsChecked = false;
                if (selectedRoute == "leave")
                {
                    if ((wsBox.Text ?? "").Trim() == defaultWorkspaceId)
                    {
                        wsBox.Text = "";
                    }
                    wsLabel.Text = "Workspace ID（可留空）";
                }
                else
                {
                    if (string.IsNullOrWhiteSpace(wsBox.Text))
                    {
                        wsBox.Text = defaultWorkspaceId;
                    }
                    wsLabel.Text = "Workspace ID";
                }
            };

            var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
            var ok = new Button { Content = "开始", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            var cancel = new Button { Content = "取消", Width = 72, Margin = new Thickness(8, 0, 0, 0) };
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 5);
            Grid.SetColumnSpan(actions, 2);
            root.Children.Add(actions);

            K12Options selected = null;
            ok.Click += (_, __) =>
            {
                string ids = (wsBox.Text ?? "").Trim();
                string selectedRoute = ((routeBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "request";
                if (ids.Length == 0 && selectedRoute != "leave")
                {
                    ShowThemedInfoDialog("一键K12", "请填写至少一个 workspace ID。");
                    return;
                }
                selected = new K12Options
                {
                    WorkspaceIds = ids,
                    Route = selectedRoute,
                    Workers = Math.Min(8, Math.Max(1, accountCount)),
                    Retries = int.TryParse((retryBox.Text ?? "").Trim(), out int retryValue) ? Math.Max(0, retryValue) : 2,
                    RetryBackoffSeconds = double.TryParse((backoffBox.Text ?? "").Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out double backoffValue) ? Math.Max(0, backoffValue) : 5,
                    InviteTimeoutSeconds = int.TryParse((inviteTimeoutBox.Text ?? "").Trim(), out int inviteTimeoutValue) ? Math.Max(10, inviteTimeoutValue) : 240,
                    AutoAcceptInvite = autoAcceptBox.IsChecked == true
                };
                dialog.DialogResult = true;
                dialog.Close();
            };
            cancel.Click += (_, __) => { dialog.DialogResult = false; dialog.Close(); };
            dialog.Content = root;
            return dialog.ShowDialog() == true ? selected : null;
        }

        private void OneClickPay_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedRowsOrCurrent()
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog("未选择账号", "请先勾选或选择要支付的账号记录。");
                return;
            }
            string paymentMethod = ShowPaymentMethodDialog("一键支付", "支付方式");
            if (paymentMethod.Length == 0) return;
            var args = new List<string> { "--one-click-pay" };
            if (rows.Count > 1)
            {
                string emailFile = Path.Combine(Path.GetTempPath(), "oneclick_emails_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
                File.WriteAllLines(emailFile, rows.Select(r => r.Identifier.Trim()), new UTF8Encoding(false));
                args.AddRange(new[] { "--email-file", emailFile });
            }
            else
            {
                args.AddRange(new[] { "--email", rows[0].Identifier });
            }
            args.Add("--payment-method");
            args.Add(paymentMethod);
            AddProxy(args);
            RunBackend("一键支付 (" + rows.Count + ")", args);
        }

        private void ShowThemedInfoDialog(string title, string message)
        {
            var dialog = new Window
            {
                Title = title,
                Owner = this,
                Width = 390,
                MinWidth = 340,
                SizeToContent = SizeToContent.Height,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg"),
                ResizeMode = ResizeMode.NoResize
            };

            var root = new StackPanel { Margin = new Thickness(16) };
            root.Children.Add(new TextBlock
            {
                Text = title,
                FontSize = 16,
                FontWeight = FontWeights.SemiBold,
                Margin = new Thickness(0, 0, 0, 8),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain")
            });
            root.Children.Add(new TextBlock
            {
                Text = message,
                TextWrapping = TextWrapping.Wrap,
                LineHeight = 20,
                Margin = new Thickness(0, 0, 0, 16),
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub")
            });
            var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            var ok = new Button { Content = "知道了", Width = 82, Style = (Style)FindResource("PrimaryButton") };
            ok.Click += (_, __) =>
            {
                dialog.DialogResult = true;
                dialog.Close();
            };
            actions.Children.Add(ok);
            root.Children.Add(actions);
            dialog.Content = root;
            dialog.ShowDialog();
        }

        private string ShowPaymentMethodDialog(string title, string labelText = "支付方式")
        {
            var dialog = new Window
            {
                Title = title,
                Owner = this,
                Width = 360,
                Height = 170,
                MinWidth = 320,
                MinHeight = 150,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };
            var root = new Grid { Margin = new Thickness(14) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(90) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var label = new TextBlock { Text = labelText, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var box = new ComboBox { Margin = new Thickness(0, 0, 0, 10) };
            box.Items.Add(new ComboBoxItem { Content = "PayPal", Tag = "paypal" });
            box.Items.Add(new ComboBoxItem { Content = "GoPay", Tag = "gopay" });
            box.SelectedIndex = 0;
            Grid.SetRow(label, 0);
            Grid.SetColumn(label, 0);
            Grid.SetRow(box, 0);
            Grid.SetColumn(box, 1);
            root.Children.Add(label);
            root.Children.Add(box);
            var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
            var ok = new Button { Content = "开始", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            var cancel = new Button { Content = "取消", Width = 72 };
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 1);
            Grid.SetColumnSpan(actions, 2);
            root.Children.Add(actions);
            string selected = "";
            ok.Click += (_, __) =>
            {
                selected = NormalizePaymentMethod(((box.SelectedItem as ComboBoxItem)?.Tag as string) ?? "paypal");
                dialog.DialogResult = true;
                dialog.Close();
            };
            cancel.Click += (_, __) => { dialog.DialogResult = false; dialog.Close(); };
            dialog.Content = root;
            return dialog.ShowDialog() == true ? selected : "";
        }

        private RegisterOptions ShowSelectedRegisterOptionsDialog(int selectedCount)
        {
            var dialog = new Window
            {
                Title = "选中邮箱注册+支付链接",
                Owner = this,
                Width = 430,
                Height = 256,
                MinWidth = 350,
                MinHeight = 230,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(14) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(110) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            var hint = new TextBlock
            {
                Text = "已选择 " + Math.Max(1, selectedCount).ToString() + " 个邮箱",
                Margin = new Thickness(0, 0, 0, 10),
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub")
            };
            Grid.SetRow(hint, 0);
            Grid.SetColumnSpan(hint, 2);
            root.Children.Add(hint);

            var workerLabel = new TextBlock { Text = "并发", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var workerBox = new TextBox { Text = DefaultWorkerCount().ToString(), Margin = new Thickness(0, 0, 0, 10) };
            Grid.SetRow(workerLabel, 1);
            Grid.SetColumn(workerLabel, 0);
            Grid.SetRow(workerBox, 1);
            Grid.SetColumn(workerBox, 1);
            root.Children.Add(workerLabel);
            root.Children.Add(workerBox);

            var paymentLabel = new TextBlock { Text = "生链方式", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var paymentBox = new ComboBox { Margin = new Thickness(0, 0, 0, 10) };
            paymentBox.Items.Add(new ComboBoxItem { Content = "PayPal 支付链接", Tag = "paypal" });
            paymentBox.Items.Add(new ComboBoxItem { Content = "GoPay 支付链接", Tag = "gopay" });
            paymentBox.SelectedIndex = 0;
            Grid.SetRow(paymentLabel, 2);
            Grid.SetColumn(paymentLabel, 0);
            Grid.SetRow(paymentBox, 2);
            Grid.SetColumn(paymentBox, 1);
            root.Children.Add(paymentLabel);
            root.Children.Add(paymentBox);

            var skipPaymentBox = new CheckBox
            {
                Content = "只注册，不生成支付链接",
                IsChecked = SkipPaypalLink,
                Margin = new Thickness(0, 0, 0, 10),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain")
            };
            Grid.SetRow(skipPaymentBox, 3);
            Grid.SetColumn(skipPaymentBox, 1);
            root.Children.Add(skipPaymentBox);
            skipPaymentBox.Checked += (_, __) => paymentBox.IsEnabled = false;
            skipPaymentBox.Unchecked += (_, __) => paymentBox.IsEnabled = true;
            paymentBox.IsEnabled = skipPaymentBox.IsChecked != true;

            var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
            var ok = new Button { Content = "开始", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            var cancel = new Button { Content = "取消", Width = 72 };
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 4);
            Grid.SetColumnSpan(actions, 2);
            root.Children.Add(actions);

            RegisterOptions selected = null;
            ok.Click += (_, __) =>
            {
                selected = new RegisterOptions
                {
                    Source = "pool",
                    Count = Math.Max(1, selectedCount),
                    Workers = ParsePositiveInt(workerBox.Text, 1, 20, DefaultWorkerCount()),
                    PaymentMethod = NormalizePaymentMethod(((paymentBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "paypal"),
                    SkipPaymentLink = skipPaymentBox.IsChecked == true
                };
                dialog.DialogResult = true;
                dialog.Close();
            };
            cancel.Click += (_, __) => { dialog.DialogResult = false; dialog.Close(); };
            dialog.Content = root;
            return dialog.ShowDialog() == true ? selected : null;
        }

        private RegisterOptions ShowRegisterOptionsDialog()
        {
            var dialog = new Window
            {
                Title = "一键注册+支付链接",
                Owner = this,
                Width = 420,
                Height = 326,
                MinWidth = 380,
                MinHeight = 300,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(14) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(110) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            var sourceLabel = new TextBlock { Text = "注册方式", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var sourceBox = new ComboBox { Margin = new Thickness(0, 0, 0, 10) };
            sourceBox.Items.Add(new ComboBoxItem { Content = "Chatai/邮箱池", Tag = "pool" });
            sourceBox.Items.Add(new ComboBoxItem { Content = "edu.liziai.cloud (CFWorker)", Tag = "cfworker" });
            sourceBox.Items.Add(new ComboBoxItem { Content = "📱 手机号注册 (SMSBower)", Tag = "phone" });
            sourceBox.SelectedIndex = 0;
            Grid.SetRow(sourceLabel, 0);
            Grid.SetColumn(sourceLabel, 0);
            Grid.SetRow(sourceBox, 0);
            Grid.SetColumn(sourceBox, 1);
            root.Children.Add(sourceLabel);
            root.Children.Add(sourceBox);

            var countLabel = new TextBlock { Text = "数量", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var countBox = new TextBox { Text = CountValue().ToString(), Margin = new Thickness(0, 0, 0, 10) };
            Grid.SetRow(countLabel, 1);
            Grid.SetColumn(countLabel, 0);
            Grid.SetRow(countBox, 1);
            Grid.SetColumn(countBox, 1);
            root.Children.Add(countLabel);
            root.Children.Add(countBox);

            var workerLabel = new TextBlock { Text = "并发", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var workerBox = new TextBox { Text = DefaultWorkerCount().ToString(), Margin = new Thickness(0, 0, 0, 10) };
            Grid.SetRow(workerLabel, 2);
            Grid.SetColumn(workerLabel, 0);
            Grid.SetRow(workerBox, 2);
            Grid.SetColumn(workerBox, 1);
            root.Children.Add(workerLabel);
            root.Children.Add(workerBox);

            var paymentLabel = new TextBlock { Text = "生链方式", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var paymentBox = new ComboBox { Margin = new Thickness(0, 0, 0, 10) };
            paymentBox.Items.Add(new ComboBoxItem { Content = "PayPal 支付链接", Tag = "paypal" });
            paymentBox.Items.Add(new ComboBoxItem { Content = "GoPay 支付链接", Tag = "gopay" });
            paymentBox.SelectedIndex = 0;
            Grid.SetRow(paymentLabel, 3);
            Grid.SetColumn(paymentLabel, 0);
            Grid.SetRow(paymentBox, 3);
            Grid.SetColumn(paymentBox, 1);
            root.Children.Add(paymentLabel);
            root.Children.Add(paymentBox);

            var skipPaymentBox = new CheckBox
            {
                Content = "只注册，不生成支付链接",
                IsChecked = SkipPaypalLink,
                Margin = new Thickness(0, 0, 0, 10),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain")
            };
            Grid.SetRow(skipPaymentBox, 4);
            Grid.SetColumn(skipPaymentBox, 1);
            root.Children.Add(skipPaymentBox);
            skipPaymentBox.Checked += (_, __) => paymentBox.IsEnabled = false;
            skipPaymentBox.Unchecked += (_, __) => paymentBox.IsEnabled = true;
            paymentBox.IsEnabled = skipPaymentBox.IsChecked != true;

            var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
            var ok = new Button { Content = "开始", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            var cancel = new Button { Content = "取消", Width = 72 };
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 5);
            Grid.SetColumnSpan(actions, 2);
            root.Children.Add(actions);

            RegisterOptions selected = null;
            ok.Click += (_, __) =>
            {
                int count = ParsePositiveInt(countBox.Text, 1, 200, 1);
                int workers = ParsePositiveInt(workerBox.Text, 1, 20, DefaultWorkerCount());
                selected = new RegisterOptions
                {
                    Source = ((sourceBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "pool",
                    Count = count,
                    Workers = workers,
                    PaymentMethod = NormalizePaymentMethod(((paymentBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "paypal"),
                    SkipPaymentLink = skipPaymentBox.IsChecked == true
                };
                CountText = count.ToString();
                dialog.DialogResult = true;
                dialog.Close();
            };
            cancel.Click += (_, __) => { dialog.DialogResult = false; dialog.Close(); };
            dialog.Content = root;
            return dialog.ShowDialog() == true ? selected : null;
        }

        private int ParsePositiveInt(string text, int min, int max, int fallback)
        {
            if (!int.TryParse((text ?? "").Trim(), out int value)) return fallback;
            return Math.Max(min, Math.Min(max, value));
        }

        private int DefaultWorkerCount()
        {
            return Math.Max(1, Math.Min(8, CountValue()));
        }

        private bool TryCreateSelectedMailboxFile(out string mailboxArg, out string mailboxFile, out int selectedCount)
        {
            mailboxArg = "--chatai-mailbox-file";
            mailboxFile = "";
            selectedCount = 0;
            var lines = new List<string>();
            foreach (PoolRow row in SelectedRowsOrCurrent())
            {
                string line = (row.RawLine ?? "").Trim().TrimStart('\ufeff');
                if (MailboxArgForLine(line).Length == 0)
                {
                    line = FindMailboxLineForRow(row);
                }
                if (MailboxArgForLine(line).Length > 0)
                {
                    lines.Add(line.Trim());
                }
            }
            if (lines.Count == 0) return false;

            mailboxFile = Path.Combine(Path.GetTempPath(), "selected_mailbox_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(mailboxFile, lines, new UTF8Encoding(false));
            selectedCount = lines.Count;
            return true;
        }

        private bool TryCreateSelectedUnregisteredMailboxFile(out string mailboxArg, out string mailboxFile, out int selectedCount, out int pendingRowCount)
        {
            mailboxArg = "--chatai-mailbox-file";
            mailboxFile = "";
            selectedCount = 0;
            pendingRowCount = 0;

            var lines = new List<string>();
            foreach (PoolRow row in SelectedRowsOrCurrent().Where(IsUnregisteredMailboxRow))
            {
                pendingRowCount++;
                string line = (row.RawLine ?? "").Trim().TrimStart('\ufeff');
                if (MailboxArgForLine(line).Length == 0)
                {
                    line = FindMailboxLineForRow(row);
                }
                if (MailboxArgForLine(line).Length > 0)
                {
                    lines.Add(line.Trim());
                }
            }
            if (lines.Count == 0) return false;

            mailboxFile = Path.Combine(Path.GetTempPath(), "selected_unregistered_mailbox_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(mailboxFile, lines, new UTF8Encoding(false));
            selectedCount = lines.Count;
            return true;
        }

        private bool IsUnregisteredMailboxRow(PoolRow row)
        {
            if (row == null) return false;
            if (HasRegisteredAccountState(row)) return false;
            if (IsCfWorkerRow(row)) return true;
            if (!string.IsNullOrWhiteSpace(row.MailboxLine)) return true;
            if (!string.IsNullOrWhiteSpace(row.RawRefreshToken)) return true;
            if (!string.IsNullOrWhiteSpace(row.RawLine) && MailboxArgForLine(row.RawLine).Length > 0) return true;
            return !string.IsNullOrWhiteSpace(FindMailboxLineForRow(row));
        }

        private bool HasRegisteredAccountState(PoolRow row)
        {
            string status = row.Status ?? "";
            if (IsPayPalCompletedRow(row)) return true;
            return status.Contains("已注册")
                || status.Contains("PayPal")
                || status.Contains("支付完成")
                || status.Contains("已导入")
                || status.Contains("宸叉敞鍐")
                || status.Contains("鏀粯瀹屾垚")
                || status.Contains("宸插鍏");
        }

        private string MailboxArgForLine(string line)
        {
            string value = (line ?? "").Trim().TrimStart('\ufeff');
            if (value.Length == 0 || value.StartsWith("#")) return "";
            if (value.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase)
                || value.EndsWith("@edu.liziai.cloud", StringComparison.OrdinalIgnoreCase)) return "--mailbox-file";
            if (value.Contains("----") && value.Split(new[] { "----" }, StringSplitOptions.None).Length >= 4) return "--chatai-mailbox-file";
            if (value.Contains("---") && value.Split(new[] { "---" }, StringSplitOptions.None).Length >= 3) return "--mailbox-file";
            return "";
        }

        private string FindMailboxLineForRow(PoolRow row)
        {
            if (!string.IsNullOrWhiteSpace(row?.MailboxLine)) return row.MailboxLine.Trim();

            string fromDb = FindMailboxLineFromSqlite(row);
            if (fromDb.Length > 0) return fromDb;

            string email = (row.Identifier ?? "").Trim();
            if (email.Length == 0) return "";

            var paths = new List<string> { row.SourcePath, GetChataiMailboxFilePath(), GetMailboxTokenFile() };
            foreach (string path in paths.Where(p => !string.IsNullOrWhiteSpace(p)).Distinct(StringComparer.OrdinalIgnoreCase))
            {
                if (!File.Exists(path) || !path.EndsWith(".txt", StringComparison.OrdinalIgnoreCase)) continue;
                foreach (string raw in File.ReadAllLines(path, Encoding.UTF8))
                {
                    string value = raw.Trim().TrimStart('\ufeff');
                    if ((value.StartsWith(email + "----", StringComparison.OrdinalIgnoreCase)
                        || value.StartsWith(email + "---", StringComparison.OrdinalIgnoreCase))
                        && MailboxArgForLine(value).Length > 0)
                    {
                        return value;
                    }
                }
            }
            return "";
        }

        private string FindMailboxLineFromSqlite(PoolRow row)
        {
            if (row == null || string.IsNullOrWhiteSpace(row.SourcePath) || !row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase)) return "";
            try
            {
                string sql = "SELECT raw_json FROM accounts WHERE id=" + OnlyDigits(row.RawLine);
                var rows = SqliteNative.Query(row.SourcePath, sql);
                if (rows.Count == 0 || !rows[0].TryGetValue("raw_json", out string rawJson) || string.IsNullOrWhiteSpace(rawJson)) return "";

                using JsonDocument document = JsonDocument.Parse(rawJson);
                if (!document.RootElement.TryGetProperty("mailbox", out JsonElement mailbox) || mailbox.ValueKind != JsonValueKind.Object) return "";

                string email = JsonString(mailbox, "email");
                string password = JsonString(mailbox, "password");
                string refreshToken = JsonString(mailbox, "refresh_token");
                string accessToken = JsonString(mailbox, "access_token");
                string clientId = JsonStringAny(mailbox, "client_id", "clientId", "token");
                string provider = JsonString(mailbox, "provider");
                if (email.Length == 0) return "";
                if (provider.Equals("cfworker", StringComparison.OrdinalIgnoreCase))
                {
                    return "cfworker://" + email;
                }
                if (provider.Equals("chatai", StringComparison.OrdinalIgnoreCase) || clientId.Length > 0)
                {
                    if (clientId.Length == 0 || refreshToken.Length == 0) return "";
                    return email + "----" + password + "----" + clientId + "----" + refreshToken;
                }
                if (refreshToken.Length == 0) return "";
                return email + "---" + password + "---" + refreshToken + "---" + accessToken + "---0";
            }
            catch
            {
                return "";
            }
        }

        private bool TryReadMailboxFromRawJson(string rawJson, out string provider, out string clientId, out string refreshToken, out string mailboxLine)
        {
            provider = "";
            clientId = "";
            refreshToken = "";
            mailboxLine = "";
            if (string.IsNullOrWhiteSpace(rawJson)) return false;
            try
            {
                using JsonDocument document = JsonDocument.Parse(rawJson);
                if (!document.RootElement.TryGetProperty("mailbox", out JsonElement mailbox) || mailbox.ValueKind != JsonValueKind.Object) return false;

                string email = JsonString(mailbox, "email");
                string password = JsonString(mailbox, "password");
                refreshToken = JsonString(mailbox, "refresh_token");
                string accessToken = JsonString(mailbox, "access_token");
                clientId = JsonStringAny(mailbox, "client_id", "clientId", "token");
                provider = JsonString(mailbox, "provider");
                if (email.Length == 0) return false;

                if (provider.Equals("cfworker", StringComparison.OrdinalIgnoreCase))
                {
                    mailboxLine = "cfworker://" + email;
                    return true;
                }

                if (provider.Equals("chatai", StringComparison.OrdinalIgnoreCase) || clientId.Length > 0)
                {
                    if (clientId.Length == 0 || refreshToken.Length == 0) return false;
                    mailboxLine = email + "----" + password + "----" + clientId + "----" + refreshToken;
                }
                else
                {
                    if (refreshToken.Length == 0) return false;
                    mailboxLine = email + "---" + password + "---" + refreshToken + "---" + accessToken + "---0";
                }
                return true;
            }
            catch
            {
                return false;
            }
        }

        private string JsonString(JsonElement obj, string property)
        {
            return obj.TryGetProperty(property, out JsonElement value) && value.ValueKind == JsonValueKind.String
                ? value.GetString() ?? ""
                : "";
        }
    }
}
