// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Registration, SMS, K12 and selection mailbox argument builders.
        // All CLI argument construction is delegated to BackendCommandPlanner
        // so the CLI contract lives in exactly one module that can be unit
        // tested without WPF.

        // RegisterFromPool_Click removed (2026-09-02, round 6): dead event
        // handler, no XAML subscriber. BackendCommandPlanner.CreatePoolRegistration
        // stays -- it is unit-tested on the Contracts side.

        private void ImportChataiMailbox_Click(object sender, RoutedEventArgs e)
        {
            var dialog = new Microsoft.Win32.OpenFileDialog
            {
                Filter = "文本文件 (*.txt)|*.txt|所有文件 (*.*)|*.*",
                Title = "选择邮箱文件"
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

            string targetFile = GetMailboxTokenFile();
            (int imported, int skipped) = MailboxPoolFileStore.ImportSupportedLines(targetFile, lines);
            ChataiMailboxFilePath = targetFile;
            RefreshPools();
            NotifySuccess($"导入完成：成功 {imported} 条，跳过 {skipped} 条。");
        }

        private async void ViewInbox_Click(object sender, RoutedEventArgs e)
        {
            PoolRow? row = SelectedEmailRowOrNotify("查看收件箱");
            if (row == null) return;
            string mailboxLine = await FindMailboxLineForRowAsync(row).ConfigureAwait(true);
            if (string.IsNullOrWhiteSpace(mailboxLine) || BackendCommandPlanner.MailboxArgumentForLine(mailboxLine).Length == 0)
            {
                MessageBox.Show("选中记录缺少可用的邮箱凭据或导入行。", "格式不匹配", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            ShowInboxDialog(row);
        }

        private async void OneClickRegister_Click(object sender, RoutedEventArgs e)
        {
            PendingMailboxSelection pending =
                await TryCreateSelectedUnregisteredMailboxFileAsync().ConfigureAwait(true);
            if (pending.Selection is { } pendingSelection)
            {
                RegisterOptions? selectedOptions = ShowSelectedRegisterOptionsDialog(pendingSelection.Count);
                if (selectedOptions == null) return;
                var plan = BackendCommandPlanner.CreateMailboxFileRegistration(
                    "选中未注册邮箱注册",
                    pendingSelection.Arg,
                    pendingSelection.File,
                    pendingSelection.Count,
                    selectedOptions.Workers,
                    registrationAtOnly: true,
                    GetRegistrationProxyPool(),
                    disable2fa: selectedOptions.Disable2fa,
                    checkPromotion: selectedOptions.CheckPromotion);
                RunBackend(plan.TaskName, plan.Arguments.ToList());
                return;
            }
            if (pending.PendingRowCount > 0)
            {
                ShowThemedInfoDialog("邮箱记录不完整", "选中的未注册邮箱缺少可用邮箱原始记录，无法直接注册。");
                return;
            }

            MailboxFileSelection? selected =
                await TryCreateSelectedMailboxFileAsync().ConfigureAwait(true);
            if (selected is { } selectedSelection)
            {
                RegisterOptions? selectedOptions = ShowSelectedRegisterOptionsDialog(selectedSelection.Count);
                if (selectedOptions == null) return;
                var plan = BackendCommandPlanner.CreateMailboxFileRegistration(
                    "选中邮箱注册",
                    selectedSelection.Arg,
                    selectedSelection.File,
                    selectedSelection.Count,
                    selectedOptions.Workers,
                    registrationAtOnly: true,
                    GetRegistrationProxyPool(),
                    disable2fa: selectedOptions.Disable2fa,
                    checkPromotion: selectedOptions.CheckPromotion);
                RunBackend(plan.TaskName, plan.Arguments.ToList());
                return;
            }

            RegisterOptions? options = ShowRegisterOptionsDialog();
            if (options == null) return;

            if (options.Source == "phone")
            {
                var plan = BackendCommandPlanner.CreatePhoneRegistration(
                    options.Count,
                    GetRegistrationProxyPool(),
                    disable2fa: options.Disable2fa,
                    checkPromotion: options.CheckPromotion);
                RunBackend(plan.TaskName, plan.Arguments.ToList());
                return;
            }

            if (options.Source == "cfworker")
            {
                var plan = BackendCommandPlanner.CreateCfWorkerRegistration(
                    GetConfiguredCfWorkerDomain(),
                    options.Count,
                    options.Workers,
                    GetRegistrationProxyPool(),
                    disable2fa: options.Disable2fa,
                    checkPromotion: options.CheckPromotion);
                RunBackend(plan.TaskName, plan.Arguments.ToList());
                return;
            }

            if (options.Source == "remail_target")
            {
                var plan = BackendCommandPlanner.CreateRemailTargetRegistration(
                    options.Count,
                    options.Workers,
                    GetRegistrationProxyPool(),
                    disable2fa: options.Disable2fa,
                    checkPromotion: options.CheckPromotion);
                RunBackend(plan.TaskName, plan.Arguments.ToList());
                return;
            }

            if (options.Source == "smailr")
            {
                var plan = BackendCommandPlanner.CreateSmailrRegistration(
                    GetConfiguredSmailrDomain(),
                    options.Count,
                    options.Workers,
                    GetRegistrationProxyPool(),
                    disable2fa: options.Disable2fa,
                    checkPromotion: options.CheckPromotion);
                RunBackend(plan.TaskName, plan.Arguments.ToList());
                return;
            }

            // Default: chatai mailbox file
            string mailboxFile = GetChataiMailboxFilePath();
            if (string.IsNullOrWhiteSpace(mailboxFile) || !File.Exists(mailboxFile))
            {
                ShowThemedInfoDialog("缺少邮箱文件", "未选择邮箱，且未找到 Chatai 邮箱文件。请先导入邮箱，或勾选要注册的邮箱记录。");
                return;
            }
            var defaultPlan = BackendCommandPlanner.CreateMailboxFileRegistration(
                "一键注册",
                "--chatai-mailbox-file",
                mailboxFile,
                options.Count,
                options.Workers,
                registrationAtOnly: true,
                GetRegistrationProxyPool(),
                disable2fa: options.Disable2fa,
                checkPromotion: options.CheckPromotion);
            RunBackend(defaultPlan.TaskName, defaultPlan.Arguments.ToList());
        }

        // AddRegistrationAtOnlyArgs removed (2026-09-02, round 6): no caller.

        private void AddNoPhoneRegistrationArgs(List<string> args)
        {
            args.Add("--no-phone-reuse");
        }

        private void OneClickSms_Click(object sender, RoutedEventArgs e)
            => RunUiTask(() => OneClickSmsAsync());

        private async Task OneClickSmsAsync(CancellationToken ct = default)
        {
            var rows = SelectedEmailRowsOrNotify("接码");
            if (rows.Count == 0) return;

            if (!await ShowSmsBowerOneClickDialogAsync())
            {
                return;
            }

            MailboxFileSelection? mailbox =
                await TryCreateMailboxFileAsync(rows, ct).ConfigureAwait(true);
            if (mailbox is null || mailbox.Count != rows.Count)
            {
                ShowThemedInfoDialog("未选择邮箱", "一键接码需要读取邮箱验证码。请先导入并选择包含完整邮箱凭据的账号。");
                return;
            }

            var plan = BackendCommandPlanner.CreateOneClickSms(
                mailbox.Arg,
                mailbox.File,
                rows.Select(r => r.Identifier.Trim()).ToList(),
                rows.Count == 1 ? SessionFileFor(rows[0]) : "",
                GetRegistrationProxyPool());
            // Ensure temp files are cleaned up by the coordinator
            RunBackend(plan.TaskName, plan.Arguments.ToList());
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

            ScanOptions? options = ShowScanOptionsDialog(rows.Count);
            if (options == null) return;

            var plan = BackendCommandPlanner.CreateAccountScan(
                rows.Select(r => r.Identifier.Trim()).ToList(),
                rows.Count == 1 ? SessionFileFor(rows[0]) : "",
                options.Workers,
                options.AutoRelogin,
                GetLivenessProxyPool());
            RunAccountBatchBackend(plan.TaskName, plan.Arguments.ToList(), "account_scan", rows.Count, plan.TimeoutMilliseconds);
        }

        private void CheckPromotion_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedRowsOrCurrent()
                .Where(r => r != null && !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim(), StringComparer.OrdinalIgnoreCase)
                .Select(group => group.First())
                .ToList();
            if (rows.Count == 0)
            {
                ShowThemedInfoDialog("账号优惠检测", "没有找到可检测的账号。请先勾选账号，或切换到包含账号的筛选范围。");
                return;
            }

            var plan = BackendCommandPlanner.CreatePromotionCheck(
                rows.Select(r => r.Identifier.Trim()).ToList(),
                DefaultWorkerCount(),
                GetLivenessProxyPool());
            RunAccountBatchBackend(plan.TaskName, plan.Arguments.ToList(), "account_promotion", rows.Count);
        }

        // Returns null when the operator cancels the dialog.
        private ScanOptions? ShowScanOptionsDialog(int accountCount)
        {
            var dialog = new Window
            {
                Title = "账号测活设置",
                Owner = this,
                Width = 740,
                MinWidth = 740,
                SizeToContent = SizeToContent.Height,
                ResizeMode = ResizeMode.CanResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(150) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            for (int i = 0; i < 4; i++)
            {
                root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            }

            var title = new TextBlock
            {
                Text = "测活 " + Math.Max(1, accountCount).ToString() + " 个账号。HTTP 200 表示 AT 有效，HTTP 401 表示 AT 已失效；可勾选 401 自动重登。",
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

            var autoReloginBox = new CheckBox
            {
                Content = "401 自动重登（RT / Cookie / 邮箱 OTP / OAuth）",
                IsChecked = false,
                Margin = new Thickness(0, 0, 0, 10),
                Foreground = (Brush)FindResource("TextMain")
            };
            Grid.SetRow(autoReloginBox, 2);
            Grid.SetColumn(autoReloginBox, 1);
            root.Children.Add(autoReloginBox);

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

            ScanOptions? selected = null;
            cancel.Click += (_, __) => dialog.Close();
            ok.Click += (_, __) =>
            {
                selected = new ScanOptions
                {
                    Workers = ParsePositiveInt(workerBox.Text, 1, 8, Math.Min(8, Math.Max(1, accountCount))),
                    AutoRelogin = autoReloginBox.IsChecked == true
                };
                dialog.DialogResult = true;
                dialog.Close();
            };

            dialog.Content = root;
            return dialog.ShowDialog() == true ? selected : null;
        }

        // ShowPaymentMethodDialog removed (2026-09-02, round 6): no caller. It
        // was superseded by ShowSelectedRegisterOptionsDialog / the protocol
        // payment window, which own their own payment-method pickers.

        // Returns null when the operator cancels the dialog.
        private RegisterOptions? ShowSelectedRegisterOptionsDialog(int selectedCount)
        {
            RegisterOptions? selected = null;
            Window dialog = CreateSelectedRegisterOptionsDialog(selectedCount, options => selected = options);
            return dialog.ShowDialog() == true ? selected : null;
        }

        private Window CreateSelectedRegisterOptionsDialog(int selectedCount, Action<RegisterOptions> accept)
        {
            var dialog = new Window
            {
                Title = "选中邮箱注册",
                Owner = this,
                Width = 560,
                Height = 278,
                MinWidth = 480,
                MinHeight = 260,
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

            var no2faBox = new CheckBox
            {
                Content = "关闭 2FA（不注册 TOTP）",
                IsChecked = true,
                Margin = new Thickness(0, 0, 0, 10),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain")
            };
            Grid.SetRow(no2faBox, 2);
            Grid.SetColumn(no2faBox, 1);
            root.Children.Add(no2faBox);

            var promotionBox = new CheckBox
            {
                Content = "注册完成后查询试用优惠",
                IsChecked = true,
                Margin = new Thickness(0, 0, 0, 10),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain")
            };
            Grid.SetRow(promotionBox, 3);
            Grid.SetColumn(promotionBox, 1);
            root.Children.Add(promotionBox);

            var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
            var ok = new Button { Content = "开始", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            var cancel = new Button { Content = "取消", Width = 72 };
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 4);
            Grid.SetColumnSpan(actions, 2);
            root.Children.Add(actions);

            ok.Click += (_, __) =>
            {
                var selected = new RegisterOptions
                {
                    Source = "pool",
                    Count = Math.Max(1, selectedCount),
                    Workers = ParsePositiveInt(workerBox.Text, 1, 20, DefaultWorkerCount()),
                    Disable2fa = no2faBox.IsChecked == true,
                    CheckPromotion = promotionBox.IsChecked == true
                };
                accept(selected);
                dialog.DialogResult = true;
                dialog.Close();
            };
            cancel.Click += (_, __) => { dialog.DialogResult = false; dialog.Close(); };
            dialog.Content = root;
            return dialog;
        }

        // Returns null when the operator cancels the dialog.
        private RegisterOptions? ShowRegisterOptionsDialog()
        {
            RegisterOptions? selected = null;
            Window dialog = CreateRegisterOptionsDialog(options => selected = options);
            return dialog.ShowDialog() == true ? selected : null;
        }

        private Window CreateRegisterOptionsDialog(Action<RegisterOptions> accept)
        {
            var dialog = new Window
            {
                Title = "一键注册",
                Owner = this,
                Width = 560,
                Height = 332,
                MinWidth = 480,
                MinHeight = 312,
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
            sourceBox.Items.Add(new ComboBoxItem { Content = "ReMail 邮箱", Tag = "remail_target" });
            sourceBox.Items.Add(new ComboBoxItem { Content = "Smailr 邮箱", Tag = "smailr" });
            sourceBox.Items.Add(new ComboBoxItem { Content = "Outlook/Hotmail/iCloud 邮箱池", Tag = "pool" });
            sourceBox.Items.Add(new ComboBoxItem { Content = "CF Worker 域名邮箱", Tag = "cfworker" });
            sourceBox.Items.Add(new ComboBoxItem { Content = "手机号注册", Tag = "phone" });
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

            var no2faBox = new CheckBox
            {
                Content = "关闭 2FA（不注册 TOTP）",
                IsChecked = true,
                Margin = new Thickness(0, 0, 0, 10),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain")
            };
            Grid.SetRow(no2faBox, 3);
            Grid.SetColumn(no2faBox, 1);
            root.Children.Add(no2faBox);

            var promotionBox = new CheckBox
            {
                Content = "注册完成后查询试用优惠",
                IsChecked = true,
                Margin = new Thickness(0, 0, 0, 10),
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain")
            };
            Grid.SetRow(promotionBox, 4);
            Grid.SetColumn(promotionBox, 1);
            root.Children.Add(promotionBox);

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
            Grid.SetRow(actions, 5);
            Grid.SetColumnSpan(actions, 2);
            root.Children.Add(actions);

            ok.Click += (_, __) =>
            {
                int count = ParsePositiveInt(countBox.Text, 1, 200, 1);
                int workers = ParsePositiveInt(workerBox.Text, 1, 20, DefaultWorkerCount());
                string selectedSource = ((sourceBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "pool";
                var selected = new RegisterOptions
                {
                    Source = selectedSource,
                    Count = count,
                    Workers = workers,
                    Disable2fa = no2faBox.IsChecked == true,
                    CheckPromotion = promotionBox.IsChecked == true
                };
                accept(selected);
                CountText = count.ToString();
                dialog.DialogResult = true;
                dialog.Close();
            };
            cancel.Click += (_, __) => { dialog.DialogResult = false; dialog.Close(); };
            dialog.Content = root;
            return dialog;
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

        private Task<MailboxFileSelection?> TryCreateSelectedMailboxFileAsync(CancellationToken ct = default)
        {
            return TryCreateMailboxFileAsync(SelectedRowsOrCurrent(), ct);
        }

        /// Result of building a temporary mailbox-file for a batch. Replaces the
        /// `out` parameters: an async method cannot declare them.
        private sealed record MailboxFileSelection(string Arg, string File, int Count);

        private async Task<MailboxFileSelection?> TryCreateMailboxFileAsync(IEnumerable<PoolRow> rows, CancellationToken ct = default)
        {
            var lines = new List<string>();
            var mailboxArgs = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (PoolRow row in rows ?? Enumerable.Empty<PoolRow>())
            {
                string line = (row.RawLine ?? "").Trim().TrimStart('\ufeff');
                if (BackendCommandPlanner.MailboxArgumentForLine(line).Length == 0)
                {
                    line = await FindMailboxLineForRowAsync(row, ct).ConfigureAwait(true);
                }
                string lineArg = BackendCommandPlanner.MailboxArgumentForLine(line);
                if (lineArg.Length > 0)
                {
                    lines.Add(line.Trim());
                    mailboxArgs.Add(lineArg);
                }
            }
            if (lines.Count == 0) return null;

            // The legacy parser is the compatibility superset for mixed provider selections.
            string mailboxArg = mailboxArgs.Count == 1 ? mailboxArgs.First() : "--chatai-mailbox-file";
            string mailboxFile = Path.Combine(Path.GetTempPath(), "selected_mailbox_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(mailboxFile, lines, new UTF8Encoding(false));
            return new MailboxFileSelection(mailboxArg, mailboxFile, lines.Count);
        }

        /// Selection plus the count of rows that were considered, which the
        /// caller needs to tell "nothing selected" apart from "selected rows have
        /// no usable mailbox line".
        private sealed record PendingMailboxSelection(MailboxFileSelection? Selection, int PendingRowCount);

        private async Task<PendingMailboxSelection> TryCreateSelectedUnregisteredMailboxFileAsync(CancellationToken ct = default)
        {
            var pending = new List<PoolRow>();
            foreach (PoolRow row in SelectedRowsOrCurrent())
            {
                if (await IsUnregisteredMailboxRowAsync(row, ct).ConfigureAwait(true)) pending.Add(row);
            }
            return new PendingMailboxSelection(
                await TryCreateMailboxFileAsync(pending, ct).ConfigureAwait(true),
                pending.Count);
        }

        private async Task<bool> IsUnregisteredMailboxRowAsync(PoolRow row, CancellationToken ct = default)
        {
            if (row == null) return false;
            if (HasRegisteredAccountState(row)) return false;
            if (IsCfWorkerRow(row)) return true;
            if (!string.IsNullOrWhiteSpace(row.MailboxLine)) return true;
            if (!string.IsNullOrWhiteSpace(row.RawRefreshToken)) return true;
            if (!string.IsNullOrWhiteSpace(row.RawLine) && BackendCommandPlanner.MailboxArgumentForLine(row.RawLine).Length > 0) return true;
            // Only reached when every local field is empty, so the backend
            // round-trip is the last resort rather than the common path.
            string line = await FindMailboxLineForRowAsync(row, ct).ConfigureAwait(true);
            return !string.IsNullOrWhiteSpace(line);
        }

        private bool HasRegisteredAccountState(PoolRow row)
        {
            string status = row.Status ?? "";
            return status.Contains("已注册")
                || status.Contains("PayPal")
                || status.Contains("支付完成")
                || status.Contains("已导入");
        }

        // MailboxArgForLine used to live here as a private copy of
        // BackendCommandPlanner.MailboxArgumentForLine. The two had already
        // drifted in form (literal BOM vs '﻿', array vs string Split, and a
        // different iCloud parser that was itself a one-line forward to
        // MailboxCredentialLineParser), and only the Contracts copy had tests.
        // Callers now use the shared one so the six existing tests actually
        // guard the production path.

        private async Task<string> FindMailboxLineForRowAsync(PoolRow? row, CancellationToken ct = default)
        {
            // The old body tested `row?.` and then dereferenced `row.` in the
            // same expression - defensive in shape only. An explicit early
            // return says it outright, and the compiler can then prove the rest.
            if (row == null) return "";
            if (!string.IsNullOrWhiteSpace(row.MailboxLine)) return row.MailboxLine.Trim();

            string fromDb = await FindMailboxLineFromBackendAsync(row, ct).ConfigureAwait(true);
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
                    if (matched && BackendCommandPlanner.MailboxArgumentForLine(value).Length > 0)
                    {
                        return value;
                    }
                }
            }
            return "";
        }



        private async Task<string> FindMailboxLineFromBackendAsync(PoolRow row, CancellationToken ct = default)
        {
            if (row == null) return "";
            try
            {
                // Was `.GetAwaiter().GetResult()` on the UI thread. That did not
                // deadlock (DesktopReadClient configures ConfigureAwait(false)),
                // but the resident-channel timeout is 120s, so a stalled backend
                // froze the whole window - close button included - for two
                // minutes. This was the only sync-over-async path in shipped
                // code. ConfigureAwait(true) because the caller resumes into UI
                // work; it is the default, stated explicitly so nobody "optimises"
                // it away later.
                string line = await desktopRead
                    .ReadMailboxLineAsync(OnlyDigits(row.RawLine), row.Identifier, ct)
                    .ConfigureAwait(true);
                return (line ?? "").Trim();
            }
            catch (Exception ex)
            {
                Log("读取邮箱 backend 失败：" + SensitiveDataSanitizer.Redact(ex.Message));
            }
            return "";
        }

        private string JsonString(JsonElement obj, string property)
        {
            return obj.TryGetProperty(property, out JsonElement value) && value.ValueKind == JsonValueKind.String
                ? value.GetString() ?? ""
                : "";
        }
    }
}
