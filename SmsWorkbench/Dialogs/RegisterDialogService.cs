// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// The three option dialogs the register/scan actions open.
    /// </summary>
    /// <remarks>
    /// Split from <c>MainWindow.Register.cs</c> so that partial only wires
    /// actions together. Every method takes the owner window (WPF resolves
    /// brushes and ownership through it); the default worker count and the
    /// initial batch count arrive as parameters instead of being read off the
    /// window's view model, which is what makes the dialogs constructible in a
    /// test without a <c>MainWindow</c> instance's state.
    /// </remarks>
    public interface IRegisterDialogService
    {
        /// <summary>One-click registration options; null when the operator cancels.</summary>
        RegisterOptions? ShowRegisterOptions(Window owner, int initialCount, int defaultWorkers);

        /// <summary>Options for registering the selected rows; null on cancel.</summary>
        RegisterOptions? ShowSelectedRegisterOptions(
            Window owner,
            int selectedCount,
            int checkedCount,
            int defaultWorkers);

        /// <summary>Account-scan options; null on cancel.</summary>
        ScanOptions? ShowScanOptions(Window owner, int accountCount);
    }

    public sealed class RegisterDialogService : IRegisterDialogService
    {
        public RegisterOptions? ShowRegisterOptions(Window owner, int initialCount, int defaultWorkers)
        {
            RegisterOptions? selected = null;
            Window dialog = CreateRegisterOptionsDialog(
                owner,
                initialCount,
                defaultWorkers,
                options => selected = options,
                onCountChosen: null);
            return dialog.ShowDialog() == true ? selected : null;
        }

        /// <summary>Used by the window when the accepted count must be remembered.</summary>
        internal Window CreateRegisterOptionsDialog(
            Window owner,
            int initialCount,
            int defaultWorkers,
            Action<RegisterOptions> accept,
            Action<int>? onCountChosen)
        {
            Window dialog = DialogFactory.Create(owner, Loc.T("一键注册"), 560, 332, 480, 312);
            Grid root = DialogFactory.CreateRootGrid(margin: 14);
            Grid form = DialogFactory.CreateFormGrid();
            form.VerticalAlignment = VerticalAlignment.Top;

            var sourceBox = new ComboBox { Margin = new Thickness(0, 0, 0, 10) };
            sourceBox.Items.Add(new ComboBoxItem { Content = Loc.T("ReMail 邮箱"), Tag = "remail_target" });
            sourceBox.Items.Add(new ComboBoxItem { Content = Loc.T("Smailr 邮箱"), Tag = "smailr" });
            sourceBox.Items.Add(new ComboBoxItem { Content = Loc.T("Outlook/Hotmail/iCloud 邮箱池"), Tag = "pool" });
            sourceBox.Items.Add(new ComboBoxItem { Content = Loc.T("CF Worker 域名邮箱"), Tag = "cfworker" });
            sourceBox.Items.Add(new ComboBoxItem { Content = Loc.T("手机号注册"), Tag = "phone" });
            sourceBox.SelectedIndex = 0;
            DialogFactory.AddFormRow(form, DialogFactory.CreateFormLabel(owner, Loc.T("注册方式")), sourceBox);

            var countLabel = DialogFactory.CreateFormLabel(owner, Loc.T("数量"));
            var countBox = new TextBox { Text = Math.Max(1, initialCount).ToString(), Margin = new Thickness(0, 0, 0, 10) };
            DialogFactory.AddFormRow(form, countLabel, countBox);

            var workerBox = new TextBox { Text = Math.Max(1, defaultWorkers).ToString(), Margin = new Thickness(0, 0, 0, 10) };
            DialogFactory.AddFormRow(form, DialogFactory.CreateFormLabel(owner, Loc.T("并发")), workerBox);

            CheckBox no2faBox = DialogFactory.CreateCheckBox(owner, Loc.T("关闭 2FA（不注册 TOTP）"));
            DialogFactory.AddValueRow(form, no2faBox);

            CheckBox promotionBox = DialogFactory.CreateCheckBox(owner, Loc.T("注册完成后查询试用优惠"));
            DialogFactory.AddValueRow(form, promotionBox);

            void UpdateTargetControls()
            {
                bool targetMode = string.Equals(
                    (sourceBox.SelectedItem as ComboBoxItem)?.Tag as string,
                    "remail_target",
                    StringComparison.OrdinalIgnoreCase);
                countLabel.Text = targetMode ? Loc.T("注册数量") : Loc.T("数量");
            }
            sourceBox.SelectionChanged += (_, __) => UpdateTargetControls();
            UpdateTargetControls();

            Grid.SetRow(form, 0);
            root.Children.Add(form);

            StackPanel actions = DialogFactory.CreateActionRow();
            actions.Margin = new Thickness(0, 10, 0, 0);
            Button ok = DialogFactory.CreatePrimaryButton(owner, Loc.T("开始"), 72);
            Button cancel = DialogFactory.CreateCancelButton(width: 72);
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 1);
            root.Children.Add(actions);

            ok.Click += (_, __) =>
            {
                int count = RegisterViewModel.ParsePositiveInt(countBox.Text, 1, 200, 1);
                int workers = RegisterViewModel.ParsePositiveInt(workerBox.Text, 1, 20, defaultWorkers);
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
                onCountChosen?.Invoke(count);
                dialog.DialogResult = true;
                dialog.Close();
            };
            cancel.Click += (_, __) => { dialog.DialogResult = false; dialog.Close(); };
            dialog.Content = root;
            return dialog;
        }

        public RegisterOptions? ShowSelectedRegisterOptions(
            Window owner,
            int selectedCount,
            int checkedCount,
            int defaultWorkers)
        {
            RegisterOptions? selected = null;
            Window dialog = CreateSelectedRegisterOptionsDialog(
                owner,
                selectedCount,
                defaultWorkers,
                checkedCount,
                options => selected = options);
            return dialog.ShowDialog() == true ? selected : null;
        }

        internal Window CreateSelectedRegisterOptionsDialog(
            Window owner,
            int selectedCount,
            int defaultWorkers,
            int checkedCount,
            Action<RegisterOptions> accept)
        {
            Window dialog = DialogFactory.Create(owner, Loc.T("选中邮箱注册"), 560, 278, 480, 260);
            Grid root = DialogFactory.CreateRootGrid(margin: 14);
            Grid form = DialogFactory.CreateFormGrid();
            form.VerticalAlignment = VerticalAlignment.Top;

            int usable = Math.Max(1, selectedCount);
            // checkedCount > usable means the local unregistered filter already
            // dropped some rows (已注册 / 半注册 / 无可用邮箱行); the backend will
            // drop more (冷却中 / OTP 派发隔离 / 死路地址) before the first
            // account is even attempted.  Surfacing both numbers here kills the
            // "I checked N but the report says N-k" confusion measured on
            // 2026-09-19 (docs/audits/scans/scan-2026-09-19-selected-22-vs-reported-3of6.md).
            string hintText = checkedCount > usable
                ? Loc.F(
                    "已选择 {0} 个可注册邮箱（勾选 {1} 个，本地排除 {2} 个已注册/半注册/记录不完整；后端还会剔除冷却/隔离/死路地址）",
                    usable,
                    checkedCount,
                    checkedCount - usable)
                : Loc.F("已选择 {0} 个邮箱", usable);
            DialogFactory.AddSpanningRow(form, new TextBlock
            {
                Text = hintText,
                TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 0, 0, 10),
                Foreground = (Brush)owner.FindResource("TextSub")
            });

            var workerBox = new TextBox { Text = Math.Max(1, defaultWorkers).ToString(), Margin = new Thickness(0, 0, 0, 10) };
            DialogFactory.AddFormRow(form, DialogFactory.CreateFormLabel(owner, Loc.T("并发")), workerBox);

            CheckBox no2faBox = DialogFactory.CreateCheckBox(owner, Loc.T("关闭 2FA（不注册 TOTP）"));
            DialogFactory.AddValueRow(form, no2faBox);

            CheckBox promotionBox = DialogFactory.CreateCheckBox(owner, Loc.T("注册完成后查询试用优惠"));
            DialogFactory.AddValueRow(form, promotionBox);

            Grid.SetRow(form, 0);
            root.Children.Add(form);

            StackPanel actions = DialogFactory.CreateActionRow();
            actions.Margin = new Thickness(0, 10, 0, 0);
            Button ok = DialogFactory.CreatePrimaryButton(owner, Loc.T("开始"), 72);
            Button cancel = DialogFactory.CreateCancelButton(width: 72);
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 1);
            root.Children.Add(actions);

            ok.Click += (_, __) =>
            {
                var selected = new RegisterOptions
                {
                    Source = "pool",
                    Count = Math.Max(1, selectedCount),
                    Workers = RegisterViewModel.ParsePositiveInt(workerBox.Text, 1, 20, defaultWorkers),
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

        public ScanOptions? ShowScanOptions(Window owner, int accountCount)
        {
            var dialog = new Window
            {
                Title = Loc.T("账号测活设置"),
                Owner = owner,
                Width = 740,
                MinWidth = 740,
                SizeToContent = SizeToContent.Height,
                ResizeMode = ResizeMode.CanResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)owner.FindResource("AppBg")
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
                Text = Loc.F("测活 {0} 个账号。HTTP 200 表示 AT 有效，HTTP 401 表示 AT 已失效；可勾选 401 自动重登。", Math.Max(1, accountCount)),
                FontSize = (double)owner.FindResource("Type.Subtitle.Size"),
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)owner.FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 14)
            };
            Grid.SetRow(title, 0);
            Grid.SetColumnSpan(title, 2);
            root.Children.Add(title);

            var workerLabel = new TextBlock
            {
                Text = Loc.T("并发数"),
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(0, 0, 10, 10),
                Foreground = (Brush)owner.FindResource("TextSub")
            };
            Grid.SetRow(workerLabel, 1);
            Grid.SetColumn(workerLabel, 0);
            root.Children.Add(workerLabel);
            var workerBox = new TextBox { Text = Math.Min(8, Math.Max(1, accountCount)).ToString(), Margin = new Thickness(0, 0, 0, 10) };
            Grid.SetRow(workerBox, 1);
            Grid.SetColumn(workerBox, 1);
            root.Children.Add(workerBox);

            var autoReloginBox = new CheckBox
            {
                Content = Loc.T("401 自动重登（RT / Cookie / 邮箱 OTP / OAuth）"),
                IsChecked = false,
                Margin = new Thickness(0, 0, 0, 10),
                Foreground = (Brush)owner.FindResource("TextMain")
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
            var cancel = new Button
            {
                Content = Loc.T("取消"),
                Width = 82,
                Margin = new Thickness(0, 0, 10, 0),
                Style = (Style)owner.FindResource("SecondaryButton")
            };
            var ok = new Button
            {
                Content = Loc.T("开始测活"),
                Width = 98,
                Style = (Style)owner.FindResource("PrimaryButton")
            };
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
                    Workers = RegisterViewModel.ParsePositiveInt(workerBox.Text, 1, 8, Math.Min(8, Math.Max(1, accountCount))),
                    AutoRelogin = autoReloginBox.IsChecked == true
                };
                dialog.DialogResult = true;
                dialog.Close();
            };

            dialog.Content = root;
            return dialog.ShowDialog() == true ? selected : null;
        }
    }
}
