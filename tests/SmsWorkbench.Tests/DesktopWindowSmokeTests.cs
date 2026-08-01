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
        FlushDispatcher();

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
        FlushDispatcher();

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
        FlushDispatcher();
        settings.UpdateLayout();
        FlushDispatcher();

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
                && field.Key is "registration_proxy_pool" or "protocol_proxy_pool")
            .ToArray();
        Assert.Equal(2, proxyEditors.Length);
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

    [Fact]
    public void SettingsPaymentAndSharedControlsLoadOnStaThread()
    {
        RunOnSta(() =>
        {
            using var fixture = new TemporaryDirectory();
            File.WriteAllText(
                Path.Combine(fixture.Path, "config.json"),
                "{\"protocol_payments\":{\"matrix\":{\"cells\":[]}}}");
            var application = CreateApplication();
            var launcher = new StubFileLauncher();

            VerifyComboBoxPopup();
            VerifyScrollableEditorsAndTables();

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
            VerifySettingsLayout(settings, settingsViewModel);
            settings.Close();

            var payment = new PaymentBatchWindow(new PaymentBatchViewModel(
                new WindowPaymentBatchService(),
                launcher,
                new[] { new PaymentBatchAccount("smoke@example.com", true) }));
            payment.Show();
            payment.UpdateLayout();
            Assert.True(payment.ActualWidth >= payment.MinWidth);
            Assert.True(payment.ActualHeight >= payment.MinHeight);
            Assert.DoesNotContain(
                ((PaymentBatchViewModel)payment.DataContext).PaymentMethodOptions,
                option => option.Id == "blik");
            payment.Close();
            application.Shutdown();
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
        => Dispatcher.CurrentDispatcher.Invoke(() => { }, DispatcherPriority.ApplicationIdle);

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

    private static void RunOnSta(Action action)
    {
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            try
            {
                action();
            }
            catch (Exception exception)
            {
                failure = exception;
            }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();

        Assert.True(thread.Join(TimeSpan.FromSeconds(20)), "WPF smoke test did not finish in time.");
        Assert.Null(failure);
    }

    private sealed class WindowPaymentBatchService : IPaymentBatchService
    {
        public IReadOnlyList<PaymentMatrixRow> LoadMatrix(string paymentMethod) => Array.Empty<PaymentMatrixRow>();

        public PaymentMatrixRow CreateDefaultMatrixRow(string paymentMethod) => new()
        {
            Name = "default",
            SampleSize = 1
        };

        public Task<JsonElement> RunAsync(PaymentBatchRequest request, CancellationToken cancellationToken)
            => throw new NotSupportedException();
    }
}
