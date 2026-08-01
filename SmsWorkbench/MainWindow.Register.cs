namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Registration, SMS, K12 and selection mailbox argument builders
        private void RegisterFromPool_Click(object sender, RoutedEventArgs e)
        {
            var args = new List<string> { "--count", CountValue().ToString(), "--workers", "4" };
            AddRegistrationProxy(args);
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
            NotifySuccess($"导入完成：成功 {imported} 条，跳过 {skipped} 条。");
        }

        private void ViewInbox_Click(object sender, RoutedEventArgs e)
        {
            PoolRow row = SelectedEmailRowOrNotify("查看收件箱");
            if (row == null) return;
            string mailboxLine = FindMailboxLineForRow(row);
            if (string.IsNullOrWhiteSpace(mailboxLine) || MailboxArgForLine(mailboxLine).Length == 0)
            {
                MessageBox.Show("选中记录缺少可用的邮箱凭据或导入行。", "格式不匹配", MessageBoxButton.OK, MessageBoxImage.Information);
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
                AddRegistrationProxy(pendingArgs);
                RunBackend("选中未注册邮箱注册", pendingArgs);
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
                AddRegistrationProxy(selectedArgs);
                RunBackend("选中邮箱注册", selectedArgs);
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
                AddRegistrationProxy(phoneArgs);
                RunBackend("手机号注册 (SMSBower)", phoneArgs);
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
                AddRegistrationProxy(cfArgs);
                RunBackend("CFWorker邮箱注册", cfArgs);
                return;
            }

            if (options.Source == "remail_target")
            {
                var targetArgs = new List<string>
                {
                    "--target-at200", options.Count.ToString(),
                    "--buy-remail-mailbox", "--remail-service-mode", "purchase",
                    "--workers", options.Workers.ToString(),
                    "--phone-reuse", "--phone-source", "smsbower"
                };
                AddRegistrationProxy(targetArgs);
                RunBackend("ReMail 长效邮箱注册 (" + options.Count + ")", targetArgs);
                return;
            }

            string mailboxArg = "--chatai-mailbox-file";
            string mailboxFile = GetChataiMailboxFilePath();
            int count = options.Count;
            if (string.IsNullOrWhiteSpace(mailboxFile) || !File.Exists(mailboxFile))
            {
                ShowThemedInfoDialog("缺少邮箱文件", "未选择邮箱，且未找到 Chatai 邮箱文件。请先导入邮箱，或勾选要注册的邮箱记录。");
                return;
            }
            var args = new List<string> { mailboxArg, mailboxFile, "--count", count.ToString(), "--workers", options.Workers.ToString() };
            AddRegistrationAtOnlyArgs(args);
            AddRegistrationProxy(args);
            RunBackend("一键注册", args);
        }

        private void AddRegistrationAtOnlyArgs(List<string> args)
        {
            args.Add("--registration-at-only");
            args.Add("--no-phone-reuse");
        }

        private async void OneClickSms_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedEmailRowsOrNotify("接码");
            if (rows.Count == 0) return;

            if (!await ShowSmsBowerOneClickDialogAsync())
            {
                return;
            }

            var args = new List<string> { "--one-click-sms", "--phone-source", "smsbower", "--workers", "1", "--refresh-timeout", "60" };
            if (!TryCreateMailboxFile(rows, out string mailboxArg, out string mailboxFile, out int mailboxCount)
                || mailboxCount != rows.Count)
            {
                ShowThemedInfoDialog("未选择邮箱", "一键接码需要读取邮箱验证码。请先导入并选择包含完整邮箱凭据的账号。");
                return;
            }
            args.AddRange(new[] { mailboxArg, mailboxFile });
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
            AddRegistrationProxy(args);
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
                ShowThemedInfoDialog("账号测活", "没有找到可测活的账号。请先勾选账号，或切换到包含账号的筛选范围。");
                return;
            }

            ScanOptions options = ShowScanOptionsDialog(rows.Count);
            if (options == null) return;

            var args = new List<string> { "--refresh-local-quota", "--quota-workers", options.Workers.ToString(), "--refresh-timeout", "90" };
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
            AddRegistrationProxy(args);
            RunBackend("账号测活(" + rows.Count + ")", args);
        }

        private ScanOptions ShowScanOptionsDialog(int accountCount)
        {
            var dialog = new Window
            {
                Title = "账号测活设置",
                Owner = this,
                Width = 600,
                MinWidth = 560,
                SizeToContent = SizeToContent.Height,
                ResizeMode = ResizeMode.CanResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(150) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            for (int i = 0; i < 3; i++)
            {
                root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            }

            var title = new TextBlock
            {
                Text = "测活 " + Math.Max(1, accountCount).ToString() + " 个账号。HTTP 200 表示 AT 有效，HTTP 401 表示 AT 已失效；不会自动重登。",
                FontSize = 14,
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 14)
            };
            Grid.SetRow(title, 0);
            Grid.SetColumnSpan(title, 2);
            root.Children.Add(title);

            var workerLabel = new TextBlock { Text = "并发数", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (Brush)FindResource("TextSub") };
            Grid.SetRow(workerLabel, 1);
            Grid.SetColumn(workerLabel, 0);
            root.Children.Add(workerLabel);
            var workerBox = new TextBox { Text = Math.Min(8, Math.Max(1, accountCount)).ToString(), Margin = new Thickness(0, 0, 0, 10) };
            Grid.SetRow(workerBox, 1);
            Grid.SetColumn(workerBox, 1);
            root.Children.Add(workerBox);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 8, 0, 0)
            };
            var cancel = new Button { Content = "取消", Width = 82, Margin = new Thickness(0, 0, 10, 0), Style = (Style)FindResource("SecondaryButton") };
            var ok = new Button { Content = "开始测活", Width = 98, Style = (Style)FindResource("PrimaryButton") };
            actions.Children.Add(cancel);
            actions.Children.Add(ok);
            Grid.SetRow(actions, 2);
            Grid.SetColumnSpan(actions, 2);
            root.Children.Add(actions);

            ScanOptions selected = null;
            cancel.Click += (_, __) => dialog.Close();
            ok.Click += (_, __) =>
            {
                selected = new ScanOptions
                {
                    Workers = ParsePositiveInt(workerBox.Text, 1, 8, Math.Min(8, Math.Max(1, accountCount)))
                };
                dialog.DialogResult = true;
                dialog.Close();
            };

            dialog.Content = root;
            return dialog.ShowDialog() == true ? selected : null;
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
            AddPaymentMethodItems(box);
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
                Title = "选中邮箱注册",
                Owner = this,
                Width = 560,
                Height = 196,
                MinWidth = 480,
                MinHeight = 180,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(14) };
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

            var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
            var ok = new Button { Content = "开始", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            var cancel = new Button { Content = "取消", Width = 72 };
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 2);
            Grid.SetColumnSpan(actions, 2);
            root.Children.Add(actions);

            RegisterOptions selected = null;
            ok.Click += (_, __) =>
            {
                selected = new RegisterOptions
                {
                    Source = "pool",
                    Count = Math.Max(1, selectedCount),
                    Workers = ParsePositiveInt(workerBox.Text, 1, 20, DefaultWorkerCount())
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
                Title = "一键注册",
                Owner = this,
                Width = 560,
                Height = 250,
                MinWidth = 480,
                MinHeight = 230,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(14) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(110) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            var sourceLabel = new TextBlock { Text = "注册方式", VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 10, 10), Foreground = (System.Windows.Media.Brush)FindResource("TextSub") };
            var sourceBox = new ComboBox { Margin = new Thickness(0, 0, 0, 10) };
            sourceBox.Items.Add(new ComboBoxItem { Content = "Chatai/邮箱池", Tag = "pool" });
            sourceBox.Items.Add(new ComboBoxItem { Content = "ReMail 长效邮箱", Tag = "remail_target" });
            sourceBox.Items.Add(new ComboBoxItem { Content = "CF Woker Mail", Tag = "cfworker" });
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

            void UpdateTargetControls()
            {
                bool targetMode = string.Equals((sourceBox.SelectedItem as ComboBoxItem)?.Tag as string, "remail_target", StringComparison.OrdinalIgnoreCase);
                countLabel.Text = targetMode ? "注册数量" : "数量";
            }
            sourceBox.SelectionChanged += (_, __) => UpdateTargetControls();
            UpdateTargetControls();

            var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
            var ok = new Button { Content = "开始", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            var cancel = new Button { Content = "取消", Width = 72 };
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 3);
            Grid.SetColumnSpan(actions, 2);
            root.Children.Add(actions);

            RegisterOptions selected = null;
            ok.Click += (_, __) =>
            {
                int count = ParsePositiveInt(countBox.Text, 1, 200, 1);
                int workers = ParsePositiveInt(workerBox.Text, 1, 20, DefaultWorkerCount());
                string selectedSource = ((sourceBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "pool";
                selected = new RegisterOptions
                {
                    Source = selectedSource,
                    Count = count,
                    Workers = workers
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
            return TryCreateMailboxFile(SelectedRowsOrCurrent(), out mailboxArg, out mailboxFile, out selectedCount);
        }

        private bool TryCreateMailboxFile(IEnumerable<PoolRow> rows, out string mailboxArg, out string mailboxFile, out int selectedCount)
        {
            mailboxArg = "";
            mailboxFile = "";
            selectedCount = 0;
            var lines = new List<string>();
            var mailboxArgs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (PoolRow row in rows ?? Enumerable.Empty<PoolRow>())
            {
                string line = (row.RawLine ?? "").Trim().TrimStart('\ufeff');
                if (MailboxArgForLine(line).Length == 0)
                {
                    line = FindMailboxLineForRow(row);
                }
                string lineArg = MailboxArgForLine(line);
                if (lineArg.Length > 0)
                {
                    lines.Add(line.Trim());
                    mailboxArgs.Add(lineArg);
                }
            }
            if (lines.Count == 0 || mailboxArgs.Count != 1) return false;

            mailboxArg = mailboxArgs.First();
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
                || status.Contains("已导入");
        }

        private string MailboxArgForLine(string line)
        {
            string value = (line ?? "").Trim().TrimStart('\ufeff');
            if (value.Length == 0 || value.StartsWith("#")) return "";
            if (value.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase)
                || value.EndsWith("@edu.liziai.cloud", StringComparison.OrdinalIgnoreCase)
                || value.EndsWith("@liziai.cloud", StringComparison.OrdinalIgnoreCase)) return "--mailbox-file";
            if (value.StartsWith("remail://", StringComparison.OrdinalIgnoreCase)) return "--mailbox-file";
            if (value.StartsWith("gmail://", StringComparison.OrdinalIgnoreCase)) return "--mailbox-file";
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
            var candidateEmails = new List<string> { email };

            var paths = new List<string> { row.SourcePath, GetChataiMailboxFilePath(), GetMailboxTokenFile() };
            foreach (string path in paths.Where(p => !string.IsNullOrWhiteSpace(p)).Distinct(StringComparer.OrdinalIgnoreCase))
            {
                if (!File.Exists(path) || !path.EndsWith(".txt", StringComparison.OrdinalIgnoreCase)) continue;
                foreach (string raw in File.ReadAllLines(path, Encoding.UTF8))
                {
                    string value = raw.Trim().TrimStart('\ufeff');
                    bool matched = candidateEmails.Any(candidate =>
                        value.StartsWith("gmail://" + candidate, StringComparison.OrdinalIgnoreCase)
                        || value.StartsWith(candidate + "----", StringComparison.OrdinalIgnoreCase)
                        || value.StartsWith(candidate + "---", StringComparison.OrdinalIgnoreCase));
                    if (matched && MailboxArgForLine(value).Length > 0)
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
                string loginPassword = JsonString(mailbox, "login_password");
                string refreshToken = JsonString(mailbox, "refresh_token");
                string accessToken = JsonString(mailbox, "access_token");
                string clientId = JsonStringAny(mailbox, "client_id", "clientId", "token");
                string clientSecret = JsonString(mailbox, "client_secret");
                string provider = JsonString(mailbox, "provider");
                string serviceToken = JsonString(mailbox, "token");
                string orderNo = JsonString(mailbox, "order_no");
                string purchaseId = JsonString(mailbox, "purchase_id");
                if (email.Length == 0) return "";
                if (provider.Equals("cfworker", StringComparison.OrdinalIgnoreCase))
                {
                    return "cfworker://" + email;
                }
                if (provider.Equals("remail", StringComparison.OrdinalIgnoreCase))
                {
                    return MailboxPoolFileStore.BuildReMailLine(email, serviceToken, orderNo, purchaseId);
                }
                if (provider.Equals("gmail", StringComparison.OrdinalIgnoreCase))
                {
                    if (clientId.Length > 0 && clientSecret.Length > 0 && refreshToken.Length > 0)
                    {
                        return "gmail://" + email + "----" + clientId + "----" + clientSecret + "----" + refreshToken
                            + (accessToken.Length > 0 ? "----" + accessToken : "");
                    }
                    if (password.Length > 0)
                    {
                        if (loginPassword.Length > 0)
                        {
                            return "gmail://" + email + "----" + loginPassword + "----" + password;
                        }
                        return "gmail://" + email + "---" + password;
                    }
                    return "";
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

        private bool TryReadMailboxFromRawJson(string rawJson, out string provider, out string clientId, out string refreshToken, out string token, out string mailboxLine)
        {
            provider = "";
            clientId = "";
            refreshToken = "";
            token = "";
            mailboxLine = "";
            if (string.IsNullOrWhiteSpace(rawJson)) return false;
            try
            {
                using JsonDocument document = JsonDocument.Parse(rawJson);
                if (!document.RootElement.TryGetProperty("mailbox", out JsonElement mailbox) || mailbox.ValueKind != JsonValueKind.Object) return false;

                string email = JsonString(mailbox, "email");
                string password = JsonString(mailbox, "password");
                string loginPassword = JsonString(mailbox, "login_password");
                refreshToken = JsonString(mailbox, "refresh_token");
                string accessToken = JsonString(mailbox, "access_token");
                string serviceToken = JsonString(mailbox, "token");
                token = accessToken;
                clientId = JsonStringAny(mailbox, "client_id", "clientId", "token");
                string clientSecret = JsonString(mailbox, "client_secret");
                provider = JsonString(mailbox, "provider");
                string orderNo = JsonString(mailbox, "order_no");
                string purchaseId = JsonString(mailbox, "purchase_id");
                if (email.Length == 0) return false;

                if (provider.Equals("cfworker", StringComparison.OrdinalIgnoreCase))
                {
                    mailboxLine = "cfworker://" + email;
                    return true;
                }

                if (provider.Equals("remail", StringComparison.OrdinalIgnoreCase))
                {
                    token = serviceToken;
                    clientId = "";
                    mailboxLine = MailboxPoolFileStore.BuildReMailLine(email, serviceToken, orderNo, purchaseId);
                    return mailboxLine.Length > 0;
                }

                if (provider.Equals("gmail", StringComparison.OrdinalIgnoreCase))
                {
                    if (clientId.Length > 0 && clientSecret.Length > 0 && refreshToken.Length > 0)
                    {
                        mailboxLine = "gmail://" + email + "----" + clientId + "----" + clientSecret + "----" + refreshToken
                            + (accessToken.Length > 0 ? "----" + accessToken : "");
                        return true;
                    }
                    if (password.Length > 0)
                    {
                        mailboxLine = loginPassword.Length > 0
                            ? "gmail://" + email + "----" + loginPassword + "----" + password
                            : "gmail://" + email + "---" + password;
                        return true;
                    }
                    return false;
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
