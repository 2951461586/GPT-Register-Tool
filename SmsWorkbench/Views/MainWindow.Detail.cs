// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Account detail dialog and detail formatting
        // The panel stays here: its buttons call window commands (open path, copy,
        // check alive, open PayPal). The data it shows comes from
        // AccountDetailAssembler, which is window-independent and unit-tested.
        private readonly AccountDetailAssembler accountDetail;

        private void ShowAccountDetail(PoolRow row)
            => RunUiTask(() => ShowAccountDetailAsync(row, _lifetimeCts.Token));

        private async Task ShowAccountDetailAsync(PoolRow row, CancellationToken ct = default)
        {
            if (row == null) return;
            string detail = await accountDetail.BuildDetailAsync(row, ct);
            string paypalUrl = row.PayPalUrl ?? "";
            bool hasPayPal = !string.IsNullOrWhiteSpace(paypalUrl);
            string accessToken = await accountDetail.ResolveAccessTokenAsync(row, ct);
            bool hasAccessToken = !string.IsNullOrWhiteSpace(accessToken);
            var dialog = new Window
            {
                Title = Loc.F("账号详情 - {0}", row.Identifier),
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
                FontSize = (double)FindResource("Type.Title.Size"),
                FontWeight = FontWeights.SemiBold,
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                VerticalAlignment = VerticalAlignment.Center
            });
            if (!string.IsNullOrWhiteSpace(row.Status))
            {
                titlePanel.Children.Add(new Border
                {
                    Background = (System.Windows.Media.Brush)FindResource("PrimarySoft"),
                    CornerRadius = (CornerRadius)FindResource("Corner.Xs"),
                    Padding = new Thickness(8, 2, 8, 2),
                    Margin = new Thickness(12, 0, 0, 0),
                    VerticalAlignment = VerticalAlignment.Center,
                    Child = new TextBlock
                    {
                        Text = row.Status,
                        FontSize = (double)FindResource("Type.Caption.Size"),
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

            var infoItems = new List<(string label, string value)>
            {
                (Loc.T("邮箱"), row.Identifier),
                (Loc.T("类型"), row.AccountType ?? ""),
                (Loc.T("状态"), row.Status ?? ""),
                (Loc.T("支付状态"), row.PayPalStatus ?? ""),
                (Loc.T("支付金额"), row.PayPalAmount ?? ""),
                ("Refresh Token", row.RefreshTokenStatus ?? ""),
                (Loc.T("创建时间"), row.CreatedAt ?? ""),
                (Loc.T("更新时间"), row.CompletedAt ?? ""),
            };
            // Provenance: the exit the account was registered through and the
            // fingerprint it was bound to. Both were already persisted per account
            // (identity_context) but never reached the panel, so two accounts looked
            // identical here. An account registered without a proxy has no affinity,
            // and the egress row is omitted rather than shown as an em dash -- that
            // is a real state, not missing data.
            if (row.IdentityBound)
            {
                if (!string.IsNullOrWhiteSpace(row.EgressEndpoint))
                    infoItems.Add((Loc.T("出口"), row.EgressEndpoint));
                if (!string.IsNullOrWhiteSpace(row.EgressCountry))
                    infoItems.Add((Loc.T("出口国家"), row.EgressCountry));
            }
            if (!string.IsNullOrWhiteSpace(row.FingerprintProfile))
                infoItems.Add((Loc.T("指纹"), row.FingerprintProfile));

            int summaryRows = (infoItems.Count + 1) / 2;
            for (int i = 0; i < summaryRows; i++) summaryGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            int idx = 0;
            for (int r = 0; r < summaryRows; r++)
            {
                for (int c = 0; c < 2 && idx < infoItems.Count; c++, idx++)
                {
                    var (label, value) = infoItems[idx];
                    var card = new Border
                    {
                        Background = (System.Windows.Media.Brush)FindResource("Surface.Card"),
                        CornerRadius = (CornerRadius)FindResource("Corner.Xl"),
                        Padding = new Thickness(12, 8, 12, 8),
                        Margin = new Thickness(c == 0 ? 0 : 6, r == 0 ? 0 : 6, c == 1 ? 0 : 6, 0)
                    };
                    var cardStack = new StackPanel();
                    cardStack.Children.Add(new TextBlock
                    {
                        Text = label,
                        FontSize = (double)FindResource("Type.Caption.Size"),
                        Foreground = (System.Windows.Media.Brush)FindResource("TextMuted"),
                        Margin = new Thickness(0, 0, 0, 4)
                    });
                    cardStack.Children.Add(new TextBlock
                    {
                        Text = string.IsNullOrWhiteSpace(value) ? "—" : value,
                        FontSize = (double)FindResource("Type.Body.Size"),
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
                    Background = (System.Windows.Media.Brush)FindResource("Surface.Card"),
                    CornerRadius = (CornerRadius)FindResource("Corner.Xl"),
                    Padding = new Thickness(12, 6, 12, 6),
                    Margin = new Thickness(0, 0, 0, 10)
                };
                var urlStack = new StackPanel();
                urlStack.Children.Add(new TextBlock
                {
                    Text = Loc.T("支付订阅链接"),
                    FontSize = (double)FindResource("Type.Caption.Size"),
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
                    FontSize = (double)FindResource("Type.Body.Size"),
                    Padding = new Thickness(0)
                });
                urlPanel.Child = urlStack;
                Grid.SetRow(urlPanel, 2);
                root.Children.Add(urlPanel);
            }

            // Raw detail text
            var detailBorder = new Border
            {
                Background = (System.Windows.Media.Brush)FindResource("Surface.Inset"),
                CornerRadius = (CornerRadius)FindResource("Corner.Xl"),
                Margin = new Thickness(0, 0, 0, 10)
            };
            var text = new TextBox
            {
                Text = detail,
                IsReadOnly = true,
                AcceptsReturn = true,
                TextWrapping = TextWrapping.NoWrap,
                FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                FontSize = (double)FindResource("Type.Body.Size"),
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
            var openButton = new Button { Content = Loc.T("打开源文件"), MinWidth = 100, Margin = new Thickness(0, 0, 8, 0) };
            openButton.Click += (_, __) => OpenAccountJson(row);
            leftActions.Children.Add(openButton);

            var copyAtButton = new Button { Content = Loc.T("一键复制AT"), MinWidth = 100, IsEnabled = hasAccessToken, Margin = new Thickness(0, 0, 8, 0) };
            copyAtButton.Click += (_, __) => RunUiTask(async () =>
            {
                if (!hasAccessToken) return;
                Clipboard.SetText(accessToken);
                copyAtButton.Content = Loc.T("已复制");
                await Task.Delay(1200);
                copyAtButton.Content = Loc.T("一键复制AT");
            });
            leftActions.Children.Add(copyAtButton);

            var checkAliveButton = new Button { Content = Loc.T("账号测活"), MinWidth = 100, Margin = new Thickness(0, 0, 8, 0) };
            checkAliveButton.Click += (_, __) => RunUiTask(async () =>
            {
                dialog.Close();
                await CheckAccountAliveAsync(row);
            });
            leftActions.Children.Add(checkAliveButton);

            // Right: primary actions
            var rightActions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            var openPayPalButton = new Button { Content = Loc.T("打开支付链接"), MinWidth = 120, IsEnabled = hasPayPal, Margin = new Thickness(0, 0, 8, 0) };
            openPayPalButton.Click += (_, __) => OpenPayPalUrl(paypalUrl, row.Identifier);
            var copyPayPalButton = new Button { Content = Loc.T("复制支付链接"), MinWidth = 120, IsEnabled = hasPayPal, Margin = new Thickness(0, 0, 8, 0) };
            copyPayPalButton.Click += (_, __) => CopyPayPalUrl(paypalUrl, row.Identifier);
            var closeButton = new Button { Content = Loc.T("关闭"), MinWidth = 80 };
            closeButton.Click += (_, __) => dialog.Close();
            rightActions.Children.Add(openPayPalButton);
            rightActions.Children.Add(copyPayPalButton);
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

        private void OpenAccountJson(PoolRow row)
            => RunUiTask(() => OpenAccountJsonAsync(row, _lifetimeCts.Token));

        private async Task OpenAccountJsonAsync(PoolRow row, CancellationToken ct = default)
        {
            string path = await accountDetail.ResolveJsonPathAsync(row, ct);
            if (string.IsNullOrWhiteSpace(path))
            {
                MessageBox.Show(Loc.T("未找到该账号对应的 JSON 文件。"), Loc.T("打开源文件"), MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            OpenPath(path);
        }

        // AddDetailRow removed (2026-09-02, round 6): no caller. The account
        // detail panel is built from XAML bindings now; this was the last
        // code-behind row builder.

    }
}
