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

            var settings = new SettingsWindow(new SettingsViewModel(
                new SettingsService(new TestApplicationPaths(fixture.Path)),
                launcher));
            settings.Show();
            settings.UpdateLayout();
            Assert.True(settings.ActualWidth >= settings.MinWidth);
            Assert.True(settings.ActualHeight >= settings.MinHeight);
            var secretBox = FindVisualChildren<PasswordBox>(settings).First();
            var secretField = Assert.IsType<SettingFieldViewModel>(secretBox.DataContext);
            Assert.Equal(SettingFieldKind.Secret, secretField.Kind);
            secretBox.Password = "first-edit";
            Assert.Equal("first-edit", secretField.Value);
            secretBox.Password = "second-edit";
            Assert.Equal("second-edit", secretField.Value);
            Assert.NotNull(secretBox.GetBindingExpression(PasswordBoxBinding.BoundPasswordProperty));
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
