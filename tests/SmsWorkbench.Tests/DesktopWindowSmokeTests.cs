using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Threading;

namespace SmsWorkbench.Tests;

public sealed class DesktopWindowSmokeTests
{
    private static void VerifyComboBoxPopup()
    {
        var comboBox = new ComboBox
        {
            Width = 240,
            MaxDropDownHeight = 80,
            SelectedIndex = 0,
            ItemsSource = Enumerable.Range(1, 20)
                .Select(index => $"Option {index}: {new string('x', 80)}")
                .ToArray()
        };
        var host = new Window { Width = 420, Height = 240, Content = comboBox };

        host.Show();
        host.UpdateLayout();

        var toggleButton = Assert.IsType<ToggleButton>(comboBox.Template.FindName("ToggleButton", comboBox));
        var contentSite = Assert.IsType<ContentPresenter>(comboBox.Template.FindName("ContentSite", comboBox));
        Assert.Equal(comboBox.ActualWidth, toggleButton.ActualWidth, precision: 3);
        Assert.Equal(comboBox.ActualHeight, toggleButton.ActualHeight, precision: 3);
        Assert.True(contentSite.ActualWidth > 0);

        comboBox.IsDropDownOpen = true;
        Assert.True(
            WaitForDispatcher(
                () =>
                {
                    var popup = comboBox.Template.FindName("Popup", comboBox) as Popup;
                    var border = popup?.Child as Border;
                    if (border is null)
                        return false;

                    var scrollBars = FindVisualChildren<ScrollBar>(border).ToArray();
                    var verticalBar = scrollBars.SingleOrDefault(scrollBar => scrollBar.Orientation == Orientation.Vertical);
                    var horizontalBar = scrollBars.SingleOrDefault(scrollBar => scrollBar.Orientation == Orientation.Horizontal);
                    return border.ActualWidth >= comboBox.ActualWidth
                        && verticalBar?.ActualWidth > 0
                        && horizontalBar?.ActualHeight > 0;
                },
                TimeSpan.FromSeconds(5)),
            "ComboBox popup did not finish layout in time");

        var popup = Assert.IsType<Popup>(comboBox.Template.FindName("Popup", comboBox));
        var border = Assert.IsType<Border>(popup.Child);
        Assert.True(border.ActualWidth >= comboBox.ActualWidth);

        ScrollBar verticalBar = FindVisualChildren<ScrollBar>(border)
            .Single(scrollBar => scrollBar.Orientation == Orientation.Vertical);
        ScrollBar horizontalBar = FindVisualChildren<ScrollBar>(border)
            .Single(scrollBar => scrollBar.Orientation == Orientation.Horizontal);
        Assert.True(verticalBar.ActualWidth > 0);
        Assert.True(horizontalBar.ActualHeight > 0);
        Assert.NotNull(verticalBar.Template.FindName("PART_Track", verticalBar));
        Assert.NotNull(horizontalBar.Template.FindName("PART_Track", horizontalBar));

        host.Close();
    }

    private static void VerifyScrollableEditorsAndTables()
    {
        var proxyEditor = new TextBox
        {
            Width = 260,
            Height = 90,
            AcceptsReturn = true,
            TextWrapping = TextWrapping.NoWrap,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            Text = string.Join(Environment.NewLine, Enumerable.Repeat(new string('x', 90), 12))
        };
        var editorHost = new Window { Width = 360, Height = 180, Content = proxyEditor };
        editorHost.Show();
        editorHost.UpdateLayout();

        Assert.True(proxyEditor.Focusable);
        Assert.NotNull(proxyEditor.Template.FindName("PART_ContentHost", proxyEditor));
        AssertStableScrollBars(proxyEditor, expectVertical: true, expectHorizontal: true);
        editorHost.Close();

        var dataGrid = new DataGrid
        {
            Width = 360,
            Height = 150,
            AutoGenerateColumns = false,
            ItemsSource = new[] { new { Value = "mailbox@example.com" } }
        };
        dataGrid.Columns.Add(new DataGridTextColumn { Header = "One", Binding = new System.Windows.Data.Binding("Value"), Width = 240 });
        dataGrid.Columns.Add(new DataGridTextColumn { Header = "Two", Binding = new System.Windows.Data.Binding("Value"), Width = 240 });
        dataGrid.Columns.Add(new DataGridTextColumn { Header = "Three", Binding = new System.Windows.Data.Binding("Value"), Width = 240 });
        var gridHost = new Window { Width = 460, Height = 240, Content = dataGrid };
        gridHost.Show();
        gridHost.UpdateLayout();

        AssertStableScrollBars(dataGrid, expectVertical: false, expectHorizontal: true);
        gridHost.Close();
    }

    private static void AssertStableScrollBars(
        DependencyObject owner,
        bool expectVertical,
        bool expectHorizontal)
    {
        ScrollBar verticalBar = FindVisualChildren<ScrollBar>(owner)
            .Single(scrollBar => scrollBar.Orientation == Orientation.Vertical);
        ScrollBar horizontalBar = FindVisualChildren<ScrollBar>(owner)
            .Single(scrollBar => scrollBar.Orientation == Orientation.Horizontal);
        Assert.Equal(expectVertical ? Visibility.Visible : Visibility.Collapsed, verticalBar.Visibility);
        Assert.Equal(expectHorizontal ? Visibility.Visible : Visibility.Collapsed, horizontalBar.Visibility);
        if (expectVertical)
        {
            Assert.True(verticalBar.ActualWidth > 0);
            Assert.NotNull(verticalBar.Template.FindName("PART_Track", verticalBar));
        }
        if (expectHorizontal)
        {
            Assert.True(horizontalBar.ActualHeight > 0);
            Assert.NotNull(horizontalBar.Template.FindName("PART_Track", horizontalBar));
        }
    }

    private static void VerifySettingsLayout(
        SettingsWindow settings,
        SettingsViewModel viewModel)
    {
        SettingsCategoryViewModel networkCategory = viewModel.Categories.Single(category => category.Title == "网络与支付");
        string proxyLines = string.Join(
            Environment.NewLine,
            Enumerable.Range(1, 12).Select(index => $"http://proxy-{index:D2}-{new string('x', 90)}.example:8080"));
        foreach (SettingFieldViewModel field in networkCategory.Sections
                     .SelectMany(section => section.Fields)
                     .Where(field => field.Key is "registration_proxy_pool" or "protocol_proxy_pool"))
        {
            field.Value = proxyLines;
        }

        viewModel.SelectedCategory = networkCategory;
        settings.UpdateLayout();

        FrameworkElement[] editors = FindVisualChildren<FrameworkElement>(settings)
            .Where(element => element.IsVisible
                && element.ActualWidth > 0
                && element.DataContext is SettingFieldViewModel
                && element is TextBox or PasswordBox or ComboBox)
            .ToArray();
        Assert.True(editors.Length >= 10);

        Rect[] editorBounds = editors.Select(editor => BoundsRelativeTo(editor, settings)).ToArray();
        Assert.True(editorBounds.Max(bounds => bounds.Left) - editorBounds.Min(bounds => bounds.Left) <= 0.5);
        Assert.True(editorBounds.Max(bounds => bounds.Right) - editorBounds.Min(bounds => bounds.Right) <= 0.5);

        var contentScrollViewer = Assert.IsType<ScrollViewer>(settings.FindName("SettingsContentScrollViewer"));
        var outerVerticalBar = Assert.IsType<ScrollBar>(
            contentScrollViewer.Template.FindName("PART_VerticalScrollBar", contentScrollViewer));
        Assert.Equal(Visibility.Visible, outerVerticalBar.Visibility);
        Rect outerBarBounds = BoundsRelativeTo(outerVerticalBar, settings);
        Assert.All(editorBounds, bounds => Assert.True(bounds.Right <= outerBarBounds.Left - 8));

        TextBox[] proxyEditors = editors
            .OfType<TextBox>()
            .Where(editor => editor.DataContext is SettingFieldViewModel field
                && field.Key == "registration_proxy_pool")
            .ToArray();
        Assert.Single(proxyEditors);
        foreach (TextBox proxyEditor in proxyEditors)
        {
            Assert.Equal(148, proxyEditor.ActualHeight, precision: 3);
            var innerScrollViewer = Assert.IsType<ScrollViewer>(
                proxyEditor.Template.FindName("PART_ContentHost", proxyEditor));
            var scrollContent = Assert.IsType<ScrollContentPresenter>(
                innerScrollViewer.Template.FindName("PART_ScrollContentPresenter", innerScrollViewer));
            var horizontalBar = Assert.IsType<ScrollBar>(
                innerScrollViewer.Template.FindName("PART_HorizontalScrollBar", innerScrollViewer));
            var verticalBar = Assert.IsType<ScrollBar>(
                innerScrollViewer.Template.FindName("PART_VerticalScrollBar", innerScrollViewer));

            Assert.Equal(Visibility.Visible, horizontalBar.Visibility);
            Assert.Equal(Visibility.Visible, verticalBar.Visibility);
            Rect contentBounds = BoundsRelativeTo(scrollContent, proxyEditor);
            Rect horizontalBounds = BoundsRelativeTo(horizontalBar, proxyEditor);
            Rect verticalBounds = BoundsRelativeTo(verticalBar, proxyEditor);
            Assert.True(contentBounds.Bottom <= horizontalBounds.Top + 0.5);
            Assert.True(contentBounds.Right <= verticalBounds.Left + 0.5);
        }

        SettingsCategoryViewModel registrationCategory = viewModel.Categories.Single(category => category.Title == "注册与接码");
        viewModel.SelectedCategory = registrationCategory;
        settings.UpdateLayout();
        var driverEditor = FindVisualChildren<ComboBox>(settings)
            .Single(editor => editor.IsVisible
                && editor.DataContext is SettingFieldViewModel field
                && field.Key == "registration_driver");
        // Keep in sync with SettingsCatalog's registration_driver options:
        // protocol / playwright / roxy / cloak / camoufox.
        Assert.Equal(5, driverEditor.Items.Count);
        Assert.True(contentScrollViewer.ScrollableHeight > 0);
        Assert.Equal(Visibility.Visible, outerVerticalBar.Visibility);
    }

    private static Rect BoundsRelativeTo(FrameworkElement element, Visual ancestor)
    {
        Point topLeft = element.TransformToAncestor(ancestor).Transform(new Point());
        return new Rect(topLeft, new Size(element.ActualWidth, element.ActualHeight));
    }

    private static void VerifyMainWindowRegistrationAndContextMenu(
        string rootDirectory,
        Action<string> stage)
    {
        stage("construct main window");
        using var logger = new Serilog.LoggerConfiguration().CreateLogger();
        var backendClient = new StubBackendClient();
        var trayIcon = new StubTrayIconService();
        var hotkey = new StubGlobalHotkeyService();
        var main = new MainWindow(
            new TestApplicationPaths(rootDirectory),
            backendClient,
            new BackendTaskCoordinator(backendClient),
            new DesktopReadClient(new BackendTaskCoordinator(backendClient)),
            new WindowPaymentBatchDialogService(),
            new WindowProxyPoolDialogService(),
            new PaymentBatchService(new TestApplicationPaths(rootDirectory), new BackendTaskCoordinator(backendClient)),
            new Wpf.Ui.SnackbarService(),
            new WindowSettingsDialogService(),
            new RegisterDialogService(),
            new SettingsService(new TestApplicationPaths(rootDirectory)),
            // The egress is the window's only direct-HTTP entry point; constructing
            // it sends nothing, so the smoke test stays offline.
            new DesktopEgress(new SettingsService(new TestApplicationPaths(rootDirectory)), logger),
            logger,
            new FileLauncher(),
            // Never hits the network; MainWindow kicks one check off on load.
            new StubUpdateChecker(),
            new WorkspaceBackupService(new TestApplicationPaths(rootDirectory)),
            trayIcon,
            hotkey);
        try
        {
            // The constructor seeds the palette but must stay silent: App.OnStartup owns
            // the one "theme applied" line. When both logged, each start printed the
            // constructor's guess ("Light", because GetAppTheme() could not answer)
            // followed by the persisted value ("Dark").
            stage("assert the constructor seeds the theme silently");
            Assert.Equal(0, CountOccurrences(main.LogPanel.Text, StartupThemeLogMarker));
            stage("assert an explicit apply logs once");
            Assert.Equal(DesktopThemeMode.System, DesktopThemePreference.LoadMode(rootDirectory));
            main.ApplyStartupTheme(DesktopThemePreference.LoadMode(rootDirectory));
            Assert.Equal(1, CountOccurrences(main.LogPanel.Text, StartupThemeLogMarker));

            stage("show main window");
            main.Show();
            main.UpdateLayout();
            stage("cycle and persist the theme mode");
            // System -> Light -> Dark -> System. The MODE is what the file stores, not
            // the resolved palette: writing the palette would pin a "follow system"
            // install the first time the OS went dark.
            main.NavCommand.Execute("theme");
            Assert.Equal(DesktopThemeMode.Light, DesktopThemePreference.LoadMode(rootDirectory));
            main.NavCommand.Execute("theme");
            Assert.Equal(DesktopThemeMode.Dark, DesktopThemePreference.LoadMode(rootDirectory));
            main.NavCommand.Execute("theme");
            Assert.Equal(DesktopThemeMode.System, DesktopThemePreference.LoadMode(rootDirectory));

            // A3 moved the sidebar into a UserControl; its namescope is the
            // control's, not the window's.
            var sidebar = Assert.Single(FindVisualChildren<Sidebar>(main));
            Assert.True(sidebar.ActualWidth > 0, "the sidebar did not get laid out");
            Assert.False(sidebar.IsCollapsed);
            var sidebarToggle = Assert.IsType<Button>(sidebar.FindName("SidebarToggleButton"));
            sidebarToggle.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Assert.True(main.SidebarCollapsed, "the toggle must route through ToggleRequested");
            sidebar.UpdateLayout();
            var firstNavButton = FindVisualChildren<IconNavButton>(sidebar).First();
            var toggleGlyph = Assert.Single(FindVisualChildren<System.Windows.Shapes.Path>(sidebarToggle));
            var firstNavGlyph = Assert.Single(FindVisualChildren<System.Windows.Shapes.Path>(firstNavButton));
            Rect toggleGlyphBounds = BoundsRelativeTo(toggleGlyph, sidebar);
            Rect firstNavGlyphBounds = BoundsRelativeTo(firstNavGlyph, sidebar);
            double toggleCenterX = toggleGlyphBounds.Left + toggleGlyphBounds.Width / 2.0;
            double navCenterX = firstNavGlyphBounds.Left + firstNavGlyphBounds.Width / 2.0;
            Assert.True(
                Math.Abs(toggleCenterX - navCenterX) <= 0.5,
                $"Collapsed sidebar toggle icon (cx={toggleCenterX:F1}) is not aligned with nav icons (cx={navCenterX:F1})");
            sidebarToggle.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Assert.False(main.SidebarCollapsed);
            var brandTitle = Assert.IsType<TextBlock>(sidebar.FindName("SidebarBrandTitle"));
            Assert.Equal("GPT Register Tool", brandTitle.Text);
            var formattedBrandTitle = new FormattedText(
                brandTitle.Text,
                System.Globalization.CultureInfo.CurrentUICulture,
                brandTitle.FlowDirection,
                new Typeface(
                    brandTitle.FontFamily,
                    brandTitle.FontStyle,
                    brandTitle.FontWeight,
                    brandTitle.FontStretch),
                brandTitle.FontSize,
                brandTitle.Foreground,
                VisualTreeHelper.GetDpi(brandTitle).PixelsPerDip);
            Assert.True(
                formattedBrandTitle.WidthIncludingTrailingWhitespace <= brandTitle.ActualWidth + 0.5,
                $"Sidebar brand title is clipped: needs {formattedBrandTitle.WidthIncludingTrailingWhitespace:F1}px, has {brandTitle.ActualWidth:F1}px");
            Assert.DoesNotContain(
                backendClient.Commands,
                command => command.Arguments.Contains("--doctor", StringComparer.Ordinal));
            main.RunStartupDoctorProbeAsync().GetAwaiter().GetResult();
            Assert.Contains(
                backendClient.Commands,
                command => command.Arguments.Contains("--doctor", StringComparer.Ordinal));
            stage("verify account grid");
            VerifyAccountScanSummary();

            // A5 moved the table into a UserControl; FindName on the window no
            // longer reaches its namescope.
            var accountGridView = Assert.Single(FindVisualChildren<AccountGridView>(main));
            Assert.True(accountGridView.ActualWidth > 0, "the account grid did not get laid out");
            var accountGrid = Assert.IsType<DataGrid>(accountGridView.FindName("AccountGrid"));
            // A4 moved the task list + log into their own collapsible segmented drawer.
            var console = Assert.Single(FindVisualChildren<WorkbenchConsole>(main));
            Assert.True(console.ActualWidth > 0, "the workbench console did not get laid out");
            Assert.NotNull(console.FindName("TaskGrid"));
            Assert.NotNull(console.FindName("LogTextBox"));
            var logPane = Assert.IsType<Border>(console.FindName("LogPane"));
            var tasksPane = Assert.IsType<Border>(console.FindName("TasksPane"));
            var tasksTab = Assert.IsType<RadioButton>(console.FindName("TasksTabButton"));
            var logTab = Assert.IsType<RadioButton>(console.FindName("LogTabButton"));
            var collapseConsoleBtn = Assert.IsType<Button>(console.FindName("CollapseToggleButton"));
            Assert.Equal(Visibility.Visible, logPane.Visibility);
            Assert.Equal(Visibility.Collapsed, tasksPane.Visibility);
            tasksTab.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            Assert.Equal(Visibility.Collapsed, logPane.Visibility);
            Assert.Equal(Visibility.Visible, tasksPane.Visibility);
            logTab.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            Assert.Equal(Visibility.Visible, logPane.Visibility);
            Assert.Equal(Visibility.Collapsed, tasksPane.Visibility);
            Assert.False(console.IsCollapsed);
            collapseConsoleBtn.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            main.UpdateLayout();
            Assert.True(console.IsCollapsed);
            Assert.Equal(36.0, Assert.IsType<RowDefinition>(main.FindName("ConsoleRow")).Height.Value);
            Assert.Equal(Visibility.Collapsed, Assert.IsType<GridSplitter>(main.FindName("ConsoleSplitter")).Visibility);
            collapseConsoleBtn.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            main.UpdateLayout();
            Assert.False(console.IsCollapsed);
            Assert.Equal(Visibility.Visible, Assert.IsType<GridSplitter>(main.FindName("ConsoleSplitter")).Visibility);
            Assert.DoesNotContain(
                FindVisualChildren<TextBlock>(main),
                textBlock => textBlock.Text == "重新生成支付链接");
            string[] headers = accountGrid.Columns.Select(column => column.Header?.ToString() ?? "").ToArray();
            Assert.DoesNotContain("ID", headers);
            Assert.DoesNotContain("注册批次", headers);
            Assert.DoesNotContain("入库", headers);
            DataGridColumn[] equalWidthColumns = accountGrid.Columns
                .Where(column => new[] { "状态", "AT", "RT", "2FA" }.Contains(column.Header?.ToString() ?? ""))
                .ToArray();
            Assert.Equal(4, equalWidthColumns.Length);
            Assert.Single(equalWidthColumns.Select(column => column.Width.Value).Distinct());
            DataGridColumn promotionColumn = Assert.Single(
                accountGrid.Columns,
                column => (column.Header?.ToString() ?? "") == "优惠状态");
            Assert.True(promotionColumn.CanUserSort);
            Assert.Equal("PromotionStatus", promotionColumn.SortMemberPath);
            Assert.DoesNotContain("支付方式", headers);

            var contextMenu = Assert.IsType<ContextMenu>(accountGrid.ContextMenu);
            contextMenu.PlacementTarget = accountGrid;
            contextMenu.Placement = PlacementMode.Center;
            stage("open account context menu");
            contextMenu.IsOpen = true;
            contextMenu.ApplyTemplate();
            contextMenu.UpdateLayout();

            Assert.NotNull(contextMenu.Template.FindName("MenuChrome", contextMenu));
            MenuItem[] menuItems = contextMenu.Items.OfType<MenuItem>().ToArray();
            Assert.True(menuItems.Length >= 8);
            // Commands reach the window through the menu's PlacementTarget; a
            // wrong binding path leaves Command null and the item silently inert.
            Assert.All(menuItems, item => Assert.NotNull(item.Command));
            // The filter bar is a UserControl now; MainWindow.FindName cannot see
            // into it, so the clear button is located through the visual tree.
            var filterBar = Assert.Single(FindVisualChildren<AccountFilterBar>(main));
            Assert.True(filterBar.ActualWidth > 0, "the account filter bar did not get laid out");
            var clearSearch = Assert.Single(
                FindVisualChildren<Button>(filterBar),
                button => ReferenceEquals(button.Command, main.Pool.ClearSearchCommand));
            Assert.Equal(Visibility.Collapsed, clearSearch.Visibility);
            main.Pool.SearchText = "abc";
            filterBar.UpdateLayout();
            Assert.Equal(Visibility.Visible, clearSearch.Visibility);
            main.Pool.SearchText = "";
            filterBar.UpdateLayout();
            Assert.Equal(Visibility.Collapsed, clearSearch.Visibility);
            var trialKpiBtn = Assert.Single(
                FindVisualChildren<Button>(filterBar),
                button => Equals(button.Tag, "trial"));
            var registeredKpiBtn = Assert.Single(
                FindVisualChildren<Button>(filterBar),
                button => Equals(button.Tag, "registered"));
            var allKpiBtn = Assert.Single(
                FindVisualChildren<Button>(filterBar),
                button => Equals(button.Tag, "all"));
            trialKpiBtn.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            Assert.Equal("trial", main.Pool.ScopeFilter);
            registeredKpiBtn.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            Assert.Equal("registered", main.Pool.ScopeFilter);
            allKpiBtn.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            Assert.Equal("all", main.Pool.ScopeFilter);
            // Unhinted Atkinson Hyperlegible Next needs Ideal + ClearType (2D symmetric AA).
            Assert.Equal(TextFormattingMode.Ideal, TextOptions.GetTextFormattingMode(main));
            Assert.Equal(TextRenderingMode.ClearType, TextOptions.GetTextRenderingMode(main));
            // Embedded Atkinson Hyperlegible Next + new DeepAI-inspired surface/shadow tokens.
            var uiFont = Assert.IsType<FontFamily>(main.FindResource("Font.Ui"));
            Assert.Contains("Atkinson Hyperlegible Next", uiFont.Source, StringComparison.Ordinal);
            Assert.IsType<SolidColorBrush>(main.FindResource("Surface.Terminal"));
            Assert.IsType<SolidColorBrush>(main.FindResource("WorkspaceFrameOutline"));
            Assert.IsType<SolidColorBrush>(main.FindResource("IconMuted"));
            Assert.IsType<Color>(main.FindResource("ShadowColor"));
            // A0 moved the window resources into a merged dictionary; a broken
            // Source or an unmerged dictionary only fails at load time.
            // The sidebar styles now live in the Sidebar control's own dictionary.
            Assert.NotNull(sidebar.FindResource("SidebarNavButtonStyle"));
            Assert.NotNull(main.FindResource("ToolbarIconPath"));
            Assert.NotNull(main.FindResource("BooleanToVisibilityConverter"));
            // A2 moved the title bar into its own control; the three buttons live
            // in the control's namescope, not the window's.
            var titleBar = Assert.Single(FindVisualChildren<TitleBar>(main));
            Assert.True(titleBar.ActualWidth > 0, "the title bar did not get laid out");
            var minBtn = Assert.IsType<Button>(titleBar.FindName("MinimizeButton"));
            var maxBtn = Assert.IsType<Button>(titleBar.FindName("MaximizeButton"));
            var closeBtn = Assert.IsType<Button>(titleBar.FindName("CloseButton"));
            Assert.Equal(46.0, minBtn.ActualWidth);
            Assert.Equal(46.0, maxBtn.ActualWidth);
            Assert.Equal(46.0, closeBtn.ActualWidth);
            Assert.Equal(maxBtn.ActualHeight, minBtn.ActualHeight);
            Assert.Equal(maxBtn.ActualHeight, closeBtn.ActualHeight);
            Assert.DoesNotContain(FindVisualChildren<TextBlock>(titleBar), tb => tb.Text == "GRT");
            Assert.Null(titleBar.FindName("VersionButton"));
            Assert.Null(titleBar.FindName("UpdateButton"));

            var brandLogoButton = Assert.IsType<Button>(sidebar.FindName("SidebarBrandLogoButton"));
            var versionPopup = Assert.IsType<System.Windows.Controls.Primitives.Popup>(sidebar.FindName("VersionPopup"));
            Assert.NotNull(sidebar.FindName("VersionButton"));
            Assert.NotNull(sidebar.FindName("UpdateButton"));
            Assert.False(versionPopup.IsOpen);
            brandLogoButton.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            Assert.True(versionPopup.IsOpen);
            versionPopup.IsOpen = false;
            var headerLeftEdges = new List<double>();
            foreach (MenuItem menuItem in menuItems)
            {
                menuItem.ApplyTemplate();
                var icon = Assert.IsType<ContentPresenter>(menuItem.Template.FindName("IconPresenter", menuItem));
                var header = Assert.IsType<TextBlock>(menuItem.Template.FindName("HeaderText", menuItem));
                Rect iconBounds = BoundsRelativeTo(icon, menuItem);
                Rect headerBounds = BoundsRelativeTo(header, menuItem);
                Assert.True(Math.Abs(iconBounds.Top + iconBounds.Height / 2 - (headerBounds.Top + headerBounds.Height / 2)) <= 0.5);
                headerLeftEdges.Add(headerBounds.Left);
            }
            Assert.True(headerLeftEdges.Max() - headerLeftEdges.Min() <= 0.5);
            contextMenu.IsOpen = false;

            stage("show general registration dialog");
            string[] sourceOptions = Array.Empty<string>();
            string[] fieldLabels = Array.Empty<string>();
            string[] checkBoxLabels = Array.Empty<string>();
            int comboBoxCount = 0;
            // The dialog now lives in RegisterDialogService; `InternalsVisibleTo`
            // lets the test build it without showing it.
            var registerDialogs = new RegisterDialogService();
            var registerDialog = Assert.IsAssignableFrom<Window>(registerDialogs.CreateRegisterOptionsDialog(
                main,
                initialCount: 1,
                defaultWorkers: 4,
                accept: _ => { },
                onCountChosen: null));
            InspectWindow(
                registerDialog,
                dialog =>
                {
                    ComboBox[] comboBoxes = FindVisualChildren<ComboBox>(dialog).ToArray();
                    comboBoxCount = comboBoxes.Length;
                    ComboBox sourceBox = comboBoxes.First();
                    sourceBox.SelectedIndex = 1;
                    dialog.UpdateLayout();
                    sourceOptions = sourceBox.Items
                        .OfType<ComboBoxItem>()
                        .Select(item => item.Content?.ToString() ?? "")
                        .ToArray();
                    fieldLabels = FindVisualChildren<TextBlock>(dialog)
                        .Select(label => label.Text)
                        .Where(text => !string.IsNullOrWhiteSpace(text))
                        .ToArray();
                    checkBoxLabels = FindVisualChildren<CheckBox>(dialog)
                        .Select(checkBox => checkBox.Content?.ToString() ?? "")
                        .Where(text => !string.IsNullOrWhiteSpace(text))
                        .ToArray();
                });
            Assert.Equal(new[]
            {
                "ReMail 邮箱",
                "Smailr 邮箱",
                "Outlook/Hotmail/iCloud 邮箱池",
                "CF Worker 域名邮箱",
                "手机号注册"
            }, sourceOptions);
            Assert.DoesNotContain(sourceOptions, option => option.Contains("📱", StringComparison.Ordinal));
            Assert.DoesNotContain("liziai.cloud (CFWorker)", sourceOptions);
            Assert.DoesNotContain("邮箱采购上限", fieldLabels);
            Assert.DoesNotContain("最大采购成本", fieldLabels);
            Assert.DoesNotContain("注册批次 ID", fieldLabels);
            Assert.DoesNotContain("生链方式", fieldLabels);
            Assert.DoesNotContain("只注册，不生成支付链接", checkBoxLabels);
            Assert.Contains("关闭 2FA（不注册 TOTP）", checkBoxLabels);
            Assert.Contains("注册完成后查询试用优惠", checkBoxLabels);
            Assert.Equal(1, comboBoxCount);

            stage("show selected registration dialog");
            int selectedComboBoxCount = -1;
            int selectedCheckBoxCount = -1;
            string[] selectedDialogTexts = Array.Empty<string>();
            var selectedRegisterDialog = Assert.IsAssignableFrom<Window>(registerDialogs.CreateSelectedRegisterOptionsDialog(
                main,
                selectedCount: 1,
                defaultWorkers: 4,
                checkedCount: 3,
                accept: _ => { }));
            InspectWindow(
                selectedRegisterDialog,
                dialog =>
                {
                    selectedComboBoxCount = FindVisualChildren<ComboBox>(dialog).Count();
                    selectedCheckBoxCount = FindVisualChildren<CheckBox>(dialog).Count();
                    selectedDialogTexts = FindVisualChildren<TextBlock>(dialog)
                        .Select(label => label.Text)
                        .Where(text => !string.IsNullOrWhiteSpace(text))
                        .ToArray();
                });
            Assert.Equal(0, selectedComboBoxCount);
            Assert.Equal(2, selectedCheckBoxCount);
            // checkedCount(3) > usable(1) must surface the local-exclusion count
            // (added 2026-09-19; see docs/audits/scans/scan-2026-09-19-selected-22-vs-reported-3of6.md).
            Assert.Contains(selectedDialogTexts, text => text.Contains("本地排除 2 个", StringComparison.Ordinal));
            stage("verify mailbox selection routing");
            VerifyMailboxSelectionFileRouting();

            // Close-to-tray: with an icon available, X hides instead of shutting down, so
            // a stray click cannot abort a running batch. The quit request (tray menu) is
            // what actually closes -- and a session without an icon (headless CI, failed
            // registration) keeps the plain close path.
            stage("close hides to the tray");
            trayIcon.IsAvailable = true;
            main.Close();
            Assert.False(main.IsVisible, "closing with a tray icon must hide the window");
            Assert.True(main.IsLoaded, "closing with a tray icon must not tear the window down");
            Assert.Equal(1, trayIcon.HiddenNotifications);

            stage("tray quit closes for real");
            trayIcon.RaiseQuitRequested();
            main.Close();
            Assert.True(hotkey.Disposed, "the hotkey must be released when the shell really closes");
        }
        finally
        {
            stage("close main window");
            main.Close();
        }
    }

    /// <summary>
    /// The account-detail panel is XAML since 2026-11-20 and its window actions
    /// arrive through <see cref="IAccountDetailActions"/>. Both halves are asserted
    /// here: the bindings render what the view carries, and each button reaches
    /// exactly one command.
    /// </summary>
    private static void VerifyAccountDetailDialog(Action<string> stage)
    {
        var row = new PoolRow
        {
            Identifier = "detail@example.com",
            Status = "已注册",
            PayPalUrl = "https://pay.openai.com/x",
            AccountType = "Session",
        };
        var actions = new RecordingAccountDetailActions();
        var view = new AccountDetailView
        {
            Identifier = row.Identifier,
            Status = row.Status,
            SummaryItems = new[]
            {
                new AccountDetailSummaryItem("邮箱", row.Identifier),
                new AccountDetailSummaryItem("类型", "—"),
            },
            EgressEndpoint = "us-edge:2000",
            EgressCountry = "US",
            FingerprintProfile = "win11-chrome134",
            PayPalUrl = row.PayPalUrl,
            AccessToken = "token-value",
        };

        stage("account detail dialog: bindings");
        var dialog = new AccountDetailDialog(null!, row, view, actions);
        dialog.Show();
        dialog.UpdateLayout();

        // Expected through the translation point, never as the Chinese literal: the
        // active language follows the operator's preference (first launch follows the
        // OS UI language), so a literal pin asserts what the *machine* says. It did --
        // CI's en-US runner rendered "Account details - detail@example.com" and this
        // line was the only failure of the run, while the button assertions below
        // stayed green because they already go through `Loc.T`.
        Assert.Equal(Localization.Loc.F("账号详情 - {0}", row.Identifier), dialog.Title);

        stage("account detail dialog: the title follows the language");
        // Both shipped languages, so neither this machine's culture nor a switch
        // earlier in the run can decide the result.
        try
        {
            foreach (Localization.AppLanguage language in new[] { Localization.AppLanguage.En, Localization.AppLanguage.Zh })
            {
                Localization.Loc.Instance.SetLanguage(language);
                var localized = new AccountDetailDialog(null!, row, view, actions);
                Assert.Equal(Localization.Loc.F("账号详情 - {0}", row.Identifier), localized.Title);
                localized.Close();
            }
        }
        finally
        {
            Localization.Loc.Instance.SetLanguage(Localization.AppLanguage.Zh);
        }

        Assert.Same(view, dialog.DataContext);
        var summary = FindVisualChildren<ItemsControl>(dialog).First();
        Assert.Equal(2, summary.Items.Count);
        Assert.DoesNotContain(FindVisualChildren<TextBox>(dialog), box => box.AcceptsReturn);
        var networkCard = Assert.IsType<Border>(dialog.FindName("NetworkInfoCard"));
        Assert.Equal(Visibility.Visible, networkCard.Visibility);
        Assert.Equal("us-edge:2000", Assert.IsType<TextBlock>(dialog.FindName("EgressEndpointText")).Text);
        Assert.Equal("US", Assert.IsType<TextBlock>(dialog.FindName("EgressCountryText")).Text);
        Assert.Equal("win11-chrome134", Assert.IsType<TextBlock>(dialog.FindName("FingerprintProfileText")).Text);

        Button ButtonWith(string key) => FindVisualChildren<Button>(dialog)
            .Single(button => Equals(button.Content, Localization.Loc.T(key)));

        Assert.True(ButtonWith("一键复制AT").IsEnabled);
        Assert.True(ButtonWith("打开支付链接").IsEnabled);
        Assert.True(ButtonWith("复制支付链接").IsEnabled);
        ButtonWith("关闭").RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        Assert.False(dialog.IsVisible);

        stage("account detail dialog: buttons reach their command");
        var targets = new AccountDetailDialog(null!, row, view, actions);
        targets.Show();
        targets.UpdateLayout();
        FindVisualChildren<Button>(targets).Single(b => Equals(b.Content, Localization.Loc.T("打开源文件")))
            .RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        FindVisualChildren<Button>(targets).Single(b => Equals(b.Content, Localization.Loc.T("复制支付链接")))
            .RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        FindVisualChildren<Button>(targets).Single(b => Equals(b.Content, Localization.Loc.T("打开支付链接")))
            .RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        Assert.Equal(new[] { "open-source", "copy-paypal", "open-paypal" }, actions.Calls);

        stage("account detail dialog: check-alive closes first");
        actions.Calls.Clear();
        FindVisualChildren<Button>(targets).Single(b => Equals(b.Content, Localization.Loc.T("账号测活")))
            .RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        Assert.Equal(new[] { "check-alive" }, actions.Calls);
        Assert.False(targets.IsVisible, "the probe opens its own UI; the panel must be gone");

        stage("account detail dialog: no AT disables copy and collapses network card");
        var withoutToken = new AccountDetailDialog(
            null!,
            row,
            new AccountDetailView { Identifier = row.Identifier },
            new RecordingAccountDetailActions());
        withoutToken.Show();
        withoutToken.UpdateLayout();
        Assert.False(FindVisualChildren<Button>(withoutToken).Single(b => Equals(b.Content, Localization.Loc.T("一键复制AT"))).IsEnabled);
        Assert.False(FindVisualChildren<Button>(withoutToken).Single(b => Equals(b.Content, Localization.Loc.T("打开支付链接"))).IsEnabled);
        Assert.Equal(Visibility.Collapsed, Assert.IsType<Border>(withoutToken.FindName("NetworkInfoCard")).Visibility);
        withoutToken.Close();
    }

    private static void VerifyInboxDialog(Action<string> stage)
    {
        stage("inbox dialog: single-window list and detail navigation");
        int refreshes = 0;
        var logs = new List<string>();
        var owner = new Window { AllowsTransparency = true, WindowStyle = WindowStyle.None };
        owner.Show();
        try
        {
            var dialog = new InboxDialogWindow(owner, "inbox@example.com", () => refreshes++, logs.Add);
            var message = new MailItem
            {
                ReceivedAt = "2026-11-22 14:00:00",
                From = "noreply@openai.com",
                Subject = "Your ChatGPT code is 482913",
                Body = "Use verification code 482913 to finish signing in.",
                VerificationCode = "482913",
            };
            dialog.MailItems.Add(message);
            dialog.Show();
            dialog.UpdateLayout();

            Assert.Equal(dialog.FindResource("Font.Ui"), dialog.FontFamily);
            Assert.Equal(dialog.FindResource("Type.Body.Size"), dialog.FontSize);
            var openDetailBtn = Assert.IsType<Button>(dialog.FindName("OpenDetailButton"));
            var openDetailText = FindVisualChildren<TextBlock>(openDetailBtn).First();
            Assert.Equal(dialog.FindResource("OnPrimary"), openDetailText.Foreground);

            Assert.False(dialog.IsShowingDetail);
            dialog.ShowDetail(message);
            dialog.UpdateLayout();
            Assert.True(dialog.IsShowingDetail);
            Assert.Equal(message.Subject, Assert.IsType<TextBlock>(dialog.FindName("DetailSubjectTextBlock")).Text);
            Assert.Contains("482913", Assert.IsType<TextBox>(dialog.FindName("DetailBodyTextBox")).Text);
            var copyCodeBtn = Assert.IsType<Button>(dialog.FindName("CopyCodeButton"));
            Assert.True(copyCodeBtn.IsEnabled);
            Assert.Equal(dialog.FindResource("OnPrimary"), FindVisualChildren<TextBlock>(copyCodeBtn).First().Foreground);

            Assert.IsType<Button>(dialog.FindName("BackToListButton"))
                .RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
            dialog.UpdateLayout();
            Assert.False(dialog.IsShowingDetail);
            Assert.True(dialog.IsVisible);
            Assert.Equal(0, refreshes);
            dialog.Close();
            Assert.True(owner.IsVisible, "closing InboxDialogWindow must never hide its owner");
        }
        finally
        {
            owner.Close();
        }
    }

    /// <summary>Records which detail-panel command ran, in order.</summary>
    private sealed class RecordingAccountDetailActions : IAccountDetailActions
    {
        public List<string> Calls { get; } = new();

        public void OpenSourceFile(PoolRow row) => Calls.Add("open-source");

        public void OpenPayPal(PoolRow row) => Calls.Add("open-paypal");

        public void CopyPayPal(PoolRow row) => Calls.Add("copy-paypal");

        public void CheckAlive(PoolRow row) => Calls.Add("check-alive");
    }

    /// <summary>
    /// Batch progress is now a XAML window; cancellation still fires once and a
    /// programmatic close does not masquerade as an operator cancellation.
    /// </summary>
    private static void VerifyBatchProgressDialog(Action<string> stage)
    {
        int cancellations = 0;
        var owner = new Window { AllowsTransparency = true, WindowStyle = WindowStyle.None };
        owner.Show();
        var dialog = new AccountBatchProgressWindow(owner, "Batch", 3, () => cancellations++);
        dialog.Show();
        dialog.UpdateLayout();

        Assert.Equal("0 / 3", Assert.IsType<TextBlock>(dialog.FindName("CountText")).Text);
        dialog.UpdateProgress(2, 3, "account@example.com", "step 2");
        Assert.Equal("2 / 3", Assert.IsType<TextBlock>(dialog.FindName("CountText")).Text);
        Assert.Equal(2, Assert.IsType<ProgressBar>(dialog.FindName("ProgressIndicator")).Value);
        Assert.Equal("account@example.com  step 2", Assert.IsType<TextBlock>(dialog.FindName("DetailText")).Text);

        stage("batch progress: cancel callback is once-only and window remains open");
        Assert.True(dialog.IsVisible);
        Assert.True(dialog.CancellationRequested is false);
        Button cancel = Assert.IsType<Button>(dialog.FindName("CancelButton"));
        cancel.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        Assert.Equal(1, cancellations);
        Assert.False(cancel.IsEnabled);
        Assert.True(dialog.IsVisible, "cancel requests work but must not close the progress window");
        Assert.True(dialog.CancellationRequested);

        stage("batch progress: programmatic close does not request cancellation again");
        dialog.ForceClose();
        Assert.False(dialog.IsVisible);
        Assert.Equal(1, cancellations);
        owner.Close();
    }

    /// <summary>
    /// The export-format picker is XAML since 2026-11-20. This is the test the
    /// conversion bought: the structure (9 formats, CPA preselected) and the
    /// click -> tag wiring are asserted on the real template, which the
    /// 90-line `new Grid/ComboBox` version could not offer.
    /// </summary>
    private static void VerifyExportFormatDialog(Action<string> stage)
    {
        stage("export format dialog: structure");
        var dialog = new ExportFormatDialog();
        dialog.Show();
        dialog.UpdateLayout();

        Assert.Equal(Localization.Loc.T("一键导出"), dialog.Title);
        var box = Assert.IsType<ComboBox>(dialog.FindName("FormatBox"));
        Assert.Equal(2, box.SelectedIndex);
        string?[] tags = box.Items.Cast<ComboBoxItem>().Select(item => item.Tag as string).ToArray();
        Assert.Equal(
            new[] { "txt", "json", "cpa", "sub2api", "cockpit", "9router", "codex", "axonhub", "codexmanager" },
            tags);
        Assert.Equal("CPA JSON", ((ComboBoxItem)box.Items[2]!).Content);
        TextBlock description = FindVisualChildren<TextBlock>(dialog)
            .Single(block => block.TextWrapping == TextWrapping.Wrap);
        Assert.True(description.Text.Length > 0, "the multi-format caveat must still be rendered");
        Assert.Equal(2, FindVisualChildren<Button>(dialog).Count());
        dialog.Close();

        stage("export format dialog: export returns the selected tag");
        var modal = new ExportFormatDialog();
        modal.Loaded += (_, _) => modal.Dispatcher.BeginInvoke(new Action(() =>
        {
            Button export = FindVisualChildren<Button>(modal)
                .First(button => Equals(button.Content, Localization.Loc.T("导出")));
            export.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        }));
        Assert.True(modal.ShowDialog());
        Assert.Equal("cpa", modal.SelectedFormat);

        stage("export format dialog: cancel returns empty");
        var cancelled = new ExportFormatDialog();
        cancelled.Loaded += (_, _) => cancelled.Dispatcher.BeginInvoke(new Action(() =>
        {
            Button cancel = FindVisualChildren<Button>(cancelled)
                .First(button => Equals(button.Content, Localization.Loc.T("取消")));
            cancel.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        }));
        Assert.False(cancelled.ShowDialog());
        Assert.Equal("", cancelled.SelectedFormat);
    }

    private static void VerifyAccountScanSummary()
    {
        var results = new List<Dictionary<string, object>>
        {
            new()
            {
                ["probe"] = new Dictionary<string, object>
                {
                    ["ok"] = true, ["status"] = "active", ["status_code"] = 200
                }
            },
            new()
            {
                ["probe"] = new Dictionary<string, object>
                {
                    ["ok"] = false, ["status"] = "token_invalid", ["status_code"] = 401
                }
            },
            new()
            {
                ["probe"] = new Dictionary<string, object>
                {
                    ["ok"] = false, ["status"] = "account_deactivated", ["terminal"] = true
                }
            },
            new()
            {
                ["status"] = "account_deactivated",
                ["probe"] = new Dictionary<string, object>
                {
                    ["ok"] = false, ["status"] = "unknown", ["status_code"] = 500
                }
            },
            new()
            {
                ["probe"] = new Dictionary<string, object>
                {
                    ["ok"] = false, ["status"] = "unknown", ["status_code"] = 500
                }
            }
        };
        var summary = new Dictionary<string, object>
        {
            ["relogin_attempted"] = 1,
            ["relogin_success"] = 0,
            ["relogin_failed"] = 1,
            ["relogin_account_deactivated"] = 1
        };

        string formatted = ExportViewModel.FormatDirectProbeSummary(results, summary);
        Assert.Contains("总数：5", formatted);
        Assert.Contains("AT有效：1", formatted);
        Assert.Contains("AT失效：1", formatted);
        Assert.Contains("账号停用：2", formatted);
        Assert.Contains("其他失败：1", formatted);
        Assert.Contains("确认停用：1", formatted);
    }

    private static void VerifyMailboxSelectionFileRouting()
    {
        // The batch-file assembly moved to RegistrationRequestBuilder, which is
        // window-independent: build it the way the window does, with a lookup that
        // never touches the resident backend. No reflection needed any more -- the
        // record is public, so the test reads it by name.
        var requests = new RegistrationRequestBuilder(
            (row, _) => Task.FromResult(row?.RawLine ?? ""),
            _ => false);

        string icloudLine = "target@icloud.com----https://mail.example/messages/AbCd_0123-credential/target%40icloud.com";
        string chataiLine = "other@example.com----password----client-id----refresh-token";
        AssertSelectionFile(requests, new[] { icloudLine }, "--mailbox-file");
        AssertSelectionFile(requests, new[] { icloudLine, chataiLine }, "--chatai-mailbox-file");
    }

    private static void AssertSelectionFile(
        RegistrationRequestBuilder requests,
        string[] lines,
        string expectedArgument)
    {
        var rows = lines.Select(line => new PoolRow { RawLine = line }).ToArray();
        MailboxFileSelection? selection = requests
            .CreateBatchFileAsync(rows, CancellationToken.None)
            .GetAwaiter()
            .GetResult();
        Assert.NotNull(selection);
        Assert.Equal(expectedArgument, selection.Arg);
        try
        {
            Assert.Equal(lines.Length, selection.Count);
            Assert.Equal(lines, File.ReadAllLines(selection.File, System.Text.Encoding.UTF8));
        }
        finally
        {
            if (File.Exists(selection.File)) File.Delete(selection.File);
        }
    }

    [Fact]
    public void SettingsPaymentAndSharedControlsLoadOnStaThread()
    {
        RunOnSta(stage =>
        {
            stage("create application");
            using var fixture = new TemporaryDirectory();
            File.WriteAllText(
                Path.Combine(fixture.Path, "config.json"),
                "{\"protocol_payments\":{\"matrix\":{\"cells\":[]}}}");
            var application = CreateApplication();
            var launcher = new StubFileLauncher();

            stage("verify wpf-ui theme dictionary");
            VerifyWpfUiThemeDictionary();

            stage("verify combo box popup");
            VerifyComboBoxPopup();
            stage("verify scrollable controls");
            VerifyScrollableEditorsAndTables();
            stage("verify export format dialog");
            VerifyExportFormatDialog(stage);
            stage("verify account detail dialog");
            VerifyAccountDetailDialog(stage);
            stage("verify inbox dialog");
            VerifyInboxDialog(stage);
            stage("verify batch progress dialog");
            VerifyBatchProgressDialog(stage);
            stage("verify focus ring static resources");
            var focusRingStyle = Assert.IsType<Style>(application.FindResource("FocusRingStyle"));
            var focusTemplate = Assert.IsType<ControlTemplate>(
                focusRingStyle.Setters.OfType<Setter>().Single(setter => setter.Property == Control.TemplateProperty).Value);
            Assert.NotNull(focusTemplate.LoadContent());
            Assert.IsType<double>(application.FindResource("Radius.Sm"));

            stage("show settings window");
            var settingsViewModel = new SettingsViewModel(
                new SettingsService(new TestApplicationPaths(fixture.Path)),
                launcher);
            var settings = new SettingsWindow(settingsViewModel)
            {
                Width = 920,
                Height = 660
            };
            settings.Show();
            settings.UpdateLayout();
            Assert.True(settings.ActualWidth >= settings.MinWidth);
            Assert.True(settings.ActualHeight >= settings.MinHeight);
            var secretBox = FindVisualChildren<PasswordBox>(settings).First();
            var secretField = Assert.IsType<SettingFieldViewModel>(secretBox.DataContext);
            Assert.Equal(SettingFieldKind.Secret, secretField.Kind);
            Assert.True(secretBox.Focusable);
            Assert.True(secretBox.IsHitTestVisible);
            Assert.NotNull(secretBox.Template.FindName("PART_ContentHost", secretBox));
            secretBox.Password = "first-edit";
            Assert.Equal("first-edit", secretField.Value);
            secretBox.Password = "second-edit";
            Assert.Equal("second-edit", secretField.Value);
            Assert.NotNull(secretBox.GetBindingExpression(PasswordBoxBinding.BoundPasswordProperty));
            var categoryList = FindVisualChildren<ListBox>(settings)
                .Single(listBox => ReferenceEquals(listBox.ItemsSource, settingsViewModel.Categories));
            categoryList.SelectedIndex = 0;
            categoryList.UpdateLayout();
            var selectedCategory = Assert.IsType<ListBoxItem>(categoryList.ItemContainerGenerator.ContainerFromIndex(0));
            selectedCategory.ApplyTemplate();
            var selectedChrome = Assert.IsType<Border>(selectedCategory.Template.FindName("ItemChrome", selectedCategory));
            Assert.Equal(settings.FindResource("SettingsSelectionBg"), selectedChrome.Background);
            Assert.Equal(settings.FindResource("SettingsSelectionBorder"), selectedChrome.BorderBrush);
            Assert.Equal(settings.FindResource("Font.Ui"), settings.FontFamily);
            Assert.Equal(settings.FindResource("Type.Body.Size"), settings.FontSize);
            var saveButton = FindVisualChildren<Button>(settings)
                .Single(button => Equals(button.Content, Localization.Loc.T("保存")));
            Assert.Equal(settings.FindResource("OnPrimary"), FindVisualChildren<TextBlock>(saveButton).First().Foreground);
            stage("verify settings layout");
            VerifySettingsLayout(settings, settingsViewModel);
            settings.Close();

            stage("show payment window");
            var paymentViewModel = new PaymentBatchViewModel(
                new WindowPaymentBatchService(),
                launcher,
                new[] { new PaymentBatchAccount("smoke@example.com", true) });
            var payment = new PaymentBatchWindow(paymentViewModel);
            payment.Show();
            payment.UpdateLayout();
            Assert.True(payment.ActualWidth >= payment.MinWidth);
            Assert.True(payment.ActualHeight >= payment.MinHeight);
            Assert.Equal(Localization.Loc.T("支付链接提取"), payment.Title);
            Assert.DoesNotContain(
                paymentViewModel.PaymentMethodOptions,
                option => option.Id == "blik");
            Assert.Contains(
                paymentViewModel.PaymentMethodOptions,
                option => option.Id == "direct_card");
            Assert.IsType<DataGrid>(payment.FindName("ResultsGrid"));
            payment.Close();
            stage("verify main window dialogs");
            VerifyMainWindowRegistrationAndContextMenu(fixture.Path, stage);
            stage("shutdown application");
            application.Shutdown();
            stage("complete");
        });
    }


    private static App CreateApplication()
    {
        var application = new App();
        application.InitializeComponent();
        application.ShutdownMode = ShutdownMode.OnExplicitShutdown;
        return application;
    }

    /// <summary>Fragment every "启动主题已应用" line starts with.</summary>
    private const string StartupThemeLogMarker = "启动主题已应用";

    private static int CountOccurrences(string haystack, string needle)
    {
        int count = 0;
        for (int index = haystack.IndexOf(needle, StringComparison.Ordinal);
             index >= 0;
             index = haystack.IndexOf(needle, index + needle.Length, StringComparison.Ordinal))
        {
            count++;
        }
        return count;
    }

    /// <summary>
    /// App.xaml must merge Wpf.Ui's own <c>Resources/Theme/Light.xaml</c>.
    /// </summary>
    /// <remarks>
    /// <para>
    /// <c>ApplicationThemeManager.Apply</c> swaps only the merged dictionary whose
    /// Source carries both "wpf.ui;" and "theme" (see
    /// <c>Wpf.Ui.Appearance.ResourceDictionaryManager.UpdateDictionary</c>). Wpf.Ui
    /// 4.3.0's <c>Wpf.Ui.xaml</c> does not merge that pair, so before the App.xaml
    /// entry existed <c>Apply</c> returned before caching anything: <c>GetAppTheme()</c>
    /// answered <c>Unknown</c> forever, <c>Changed</c> never fired, and every WPF-UI
    /// template key this app does not override resolved to nothing.
    /// </para>
    /// <para>
    /// That is also the root of the duplicate startup log: the window constructor
    /// fell back to Light because the question could not be answered, then
    /// <c>App.OnStartup</c> applied the persisted Dark.
    /// </para>
    /// </remarks>
    private static void VerifyWpfUiThemeDictionary()
    {
        static string[] MergedSources() => Application.Current.Resources.MergedDictionaries
            .Select(dictionary => dictionary.Source?.OriginalString ?? "")
            .ToArray();

        Assert.Contains(
            MergedSources(),
            source => source.Contains("wpf.ui;", StringComparison.OrdinalIgnoreCase)
                && source.Contains("theme", StringComparison.OrdinalIgnoreCase)
                && source.EndsWith("Theme/Light.xaml", StringComparison.OrdinalIgnoreCase));

        // GetAppTheme() has to infer Light from the merged Source, which only works
        // when the dictionary the App merges is the one Wpf.Ui looks for.
        Assert.Equal(
            Wpf.Ui.Appearance.ApplicationTheme.Light,
            Wpf.Ui.Appearance.ApplicationThemeManager.GetAppTheme());

        try
        {
            Wpf.Ui.Appearance.ApplicationThemeManager.Apply(Wpf.Ui.Appearance.ApplicationTheme.Dark);
            Assert.Equal(
                Wpf.Ui.Appearance.ApplicationTheme.Dark,
                Wpf.Ui.Appearance.ApplicationThemeManager.GetAppTheme());
            Assert.Contains(
                MergedSources(),
                source => source.EndsWith("Theme/Dark.xaml", StringComparison.OrdinalIgnoreCase));
        }
        finally
        {
            // Leave the process on the theme the rest of the suite expects.
            Wpf.Ui.Appearance.ApplicationThemeManager.Apply(Wpf.Ui.Appearance.ApplicationTheme.Light);
        }

        Assert.Equal(
            Wpf.Ui.Appearance.ApplicationTheme.Light,
            Wpf.Ui.Appearance.ApplicationThemeManager.GetAppTheme());
    }

    private static void FlushDispatcher()
        => Dispatcher.CurrentDispatcher.Invoke(() => { }, DispatcherPriority.Background);

    private static bool WaitForDispatcher(Func<bool> condition, TimeSpan timeout)
    {
        Dispatcher dispatcher = Dispatcher.CurrentDispatcher;
        DateTime deadline = DateTime.UtcNow + timeout;
        while (!condition())
        {
            if (DateTime.UtcNow >= deadline)
                return false;

            // Background, not Render.
            //
            // Render-priority work is only dispatched when a real render pass
            // runs. GitHub's hosted Windows runner has no interactive desktop,
            // so the render pass never happens, the BeginInvoke below is never
            // invoked, and PushFrame blocks until the outer STA join times out
            // (which reads as "did not finish in time", not as the real cause).
            //
            // Background (priority 4) sits below Render (7) and Loaded (6), so
            // pushing a frame still drains every higher-priority item first —
            // layout included — and exits regardless of whether rendering works.
            var frame = new DispatcherFrame();
            dispatcher.BeginInvoke(
                DispatcherPriority.Background,
                new Action(() => frame.Continue = false));
            Dispatcher.PushFrame(frame);

            // With Render there was a real frame interval between iterations.
            // Without it the loop spins hot, so pace it explicitly.
            Thread.Sleep(10);
        }

        return true;
    }

    private static void InspectWindow(Window dialog, Action<Window> inspect)
    {
        try
        {
            dialog.Show();
            dialog.UpdateLayout();
            inspect(dialog);
        }
        finally
        {
            dialog.Close();
        }
    }

    private static IEnumerable<T> FindVisualChildren<T>(DependencyObject parent) where T : DependencyObject
    {
        for (int index = 0; index < VisualTreeHelper.GetChildrenCount(parent); index++)
        {
            DependencyObject child = VisualTreeHelper.GetChild(parent, index);
            if (child is T match)
                yield return match;

            foreach (T descendant in FindVisualChildren<T>(child))
                yield return descendant;
        }
    }

    private static void RunOnSta(Action<Action<string>> action)
    {
        Exception? failure = null;
        string stage = "thread startup";
        var thread = new Thread(() =>
        {
            try
            {
                action(value => stage = value);
            }
            catch (Exception exception)
            {
                failure = exception;
            }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();

        // 20 s was enough on a workstation but not on a hosted runner doing
        // software rendering. The stage name in the failure is what actually
        // identifies the hang, so a generous ceiling costs nothing but CI time.
        Assert.True(
            thread.Join(TimeSpan.FromSeconds(90)),
            "WPF smoke test did not finish in time. Last stage: " + stage);
        Assert.Null(failure);
    }

    private sealed class WindowPaymentBatchService : IPaymentBatchService
    {
        public IReadOnlyList<PaymentMatrixRow> LoadMatrix(string paymentMethod) => Array.Empty<PaymentMatrixRow>();

        public PaymentBatchProxyConfiguration LoadProxyConfiguration(string paymentMethod)
            => new("", "", "", "JP");

        public SettingsSaveResult SaveProxyConfiguration(
            string paymentMethod,
            PaymentBatchProxyConfiguration configuration)
            => new(true);

        public PaymentMatrixRow CreateDefaultMatrixRow(string paymentMethod) => new()
        {
            Name = "default",
            SampleSize = 1
        };

        public Task<JsonElement> RunAsync(
            PaymentBatchRequest request,
            CancellationToken cancellationToken)
            => throw new NotSupportedException();

        public Task<JsonElement> ProbeProxiesAsync(
            string paymentMethod,
            string checkoutProxyPool,
            string approveProxyPool,
            string checkoutCountry,
            string approveCountry,
            CancellationToken cancellationToken)
            => throw new NotSupportedException();
    }

    private sealed class WindowPaymentBatchDialogService : IPaymentBatchDialogService
    {
        public bool ShowDialog(Window owner, IEnumerable<PaymentBatchAccount> accounts) => false;
    }

    private sealed class WindowProxyPoolDialogService : IProxyPoolDialogService
    {
        public void ShowDialog(Window owner)
        {
        }
    }

    private sealed class WindowSettingsDialogService : ISettingsDialogService
    {
        public bool ShowDialog(Window owner) => false;
    }
}
