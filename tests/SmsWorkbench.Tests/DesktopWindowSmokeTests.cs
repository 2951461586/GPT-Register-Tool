using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Threading;
using SmsWorkbench;

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
        FlushDispatcher();

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
        var main = new MainWindow(
            new TestApplicationPaths(rootDirectory),
            backendClient,
            new BackendTaskCoordinator(backendClient),
            new DesktopReadClient(new BackendTaskCoordinator(backendClient)),
            new WindowPaymentBatchDialogService(),
            new PaymentBatchService(new TestApplicationPaths(rootDirectory), backendClient),
            new Wpf.Ui.SnackbarService(),
            new WindowSettingsDialogService(),
            new SettingsService(new TestApplicationPaths(rootDirectory)),
            logger);
        try
        {
            stage("show main window");
            main.Show();
            main.UpdateLayout();
            Assert.DoesNotContain(
                backendClient.Commands,
                command => command.Arguments.Contains("--doctor", StringComparer.Ordinal));
            main.RunStartupDoctorProbeAsync().GetAwaiter().GetResult();
            Assert.Contains(
                backendClient.Commands,
                command => command.Arguments.Contains("--doctor", StringComparer.Ordinal));
            stage("verify account grid");
            VerifyAccountScanSummary(main);

            var accountGrid = Assert.IsType<DataGrid>(main.FindName("AccountGrid"));
            Assert.DoesNotContain(
                FindVisualChildren<TextBlock>(main),
                textBlock => textBlock.Text == "重新生成支付链接");
            string[] headers = accountGrid.Columns.Select(column => column.Header?.ToString() ?? "").ToArray();
            Assert.DoesNotContain("注册批次", headers);
            Assert.DoesNotContain("入库", headers);
            DataGridColumn promotionColumn = Assert.Single(
                accountGrid.Columns,
                column => (column.Header?.ToString() ?? "") == "优惠状态");
            Assert.True(promotionColumn.CanUserSort);
            Assert.Equal("PromotionStatus", promotionColumn.SortMemberPath);

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
            var method = typeof(MainWindow).GetMethod(
                "CreateRegisterOptionsDialog",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
            Assert.NotNull(method);
            var registerDialog = Assert.IsType<Window>(method.Invoke(
                main,
                new object[] { new Action<RegisterOptions>(_ => { }) }));
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
            method = typeof(MainWindow).GetMethod(
                "CreateSelectedRegisterOptionsDialog",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
            Assert.NotNull(method);
            var selectedRegisterDialog = Assert.IsType<Window>(method.Invoke(
                main,
                new object[] { 1, new Action<RegisterOptions>(_ => { }) }));
            InspectWindow(
                selectedRegisterDialog,
                dialog =>
                {
                    selectedComboBoxCount = FindVisualChildren<ComboBox>(dialog).Count();
                    selectedCheckBoxCount = FindVisualChildren<CheckBox>(dialog).Count();
                });
            Assert.Equal(0, selectedComboBoxCount);
            Assert.Equal(2, selectedCheckBoxCount);
            stage("verify mailbox selection routing");
            VerifyMailboxSelectionFileRouting(main);
        }
        finally
        {
            stage("close main window");
            main.Close();
        }
    }

    private static void VerifyAccountScanSummary(MainWindow main)
    {
        var formatter = typeof(MainWindow).GetMethod(
            "FormatDirectProbeSummary",
            System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
        Assert.NotNull(formatter);

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

        string formatted = Assert.IsType<string>(formatter.Invoke(main, new object[] { results, summary }));
        Assert.Contains("总数：5", formatted);
        Assert.Contains("AT有效：1", formatted);
        Assert.Contains("AT失效：1", formatted);
        Assert.Contains("账号停用：2", formatted);
        Assert.Contains("其他失败：1", formatted);
        Assert.Contains("确认停用：1", formatted);
    }

    private static void VerifyMailboxSelectionFileRouting(MainWindow main)
    {
        var method = typeof(MainWindow).GetMethod(
            "TryCreateMailboxFile",
            System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
        Assert.NotNull(method);

        string icloudLine = "target@icloud.com----https://mail.example/messages/AbCd_0123-credential/target%40icloud.com";
        string chataiLine = "other@example.com----password----client-id----refresh-token";
        AssertSelectionFile(method, main, new[] { icloudLine }, "--mailbox-file");
        AssertSelectionFile(method, main, new[] { icloudLine, chataiLine }, "--chatai-mailbox-file");
    }

    private static void AssertSelectionFile(
        System.Reflection.MethodInfo method,
        MainWindow main,
        string[] lines,
        string expectedArgument)
    {
        var rows = lines.Select(line => new PoolRow { RawLine = line }).ToArray();
        object?[] arguments = { rows, "", "", 0 };
        Assert.True(Assert.IsType<bool>(method.Invoke(main, arguments)));
        Assert.Equal(expectedArgument, Assert.IsType<string>(arguments[1]));
        string path = Assert.IsType<string>(arguments[2]);
        try
        {
            Assert.Equal(lines.Length, Assert.IsType<int>(arguments[3]));
            Assert.Equal(lines, File.ReadAllLines(path, System.Text.Encoding.UTF8));
        }
        finally
        {
            if (File.Exists(path)) File.Delete(path);
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

            stage("verify combo box popup");
            VerifyComboBoxPopup();
            stage("verify scrollable controls");
            VerifyScrollableEditorsAndTables();

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

    private static void FlushDispatcher()
        => Dispatcher.CurrentDispatcher.Invoke(() => { }, DispatcherPriority.Background);

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

        Assert.True(
            thread.Join(TimeSpan.FromSeconds(20)),
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

    private sealed class WindowSettingsDialogService : ISettingsDialogService
    {
        public bool ShowDialog(Window owner) => false;
    }
}
