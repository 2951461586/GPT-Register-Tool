using System.Text.Json;
using System.Windows;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class DesktopWindowSmokeTests
{
    [Fact]
    public void SettingsAndPaymentBatchWindowsLoadOnStaThread()
    {
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            try
            {
                using var fixture = new TemporaryDirectory();
                File.WriteAllText(
                    Path.Combine(fixture.Path, "config.json"),
                    "{\"protocol_payments\":{\"matrix\":{\"cells\":[]}}}");
                var application = new App();
                application.InitializeComponent();
                application.ShutdownMode = ShutdownMode.OnExplicitShutdown;
                var launcher = new StubFileLauncher();

                var settings = new SettingsWindow(new SettingsViewModel(
                    new SettingsService(new TestApplicationPaths(fixture.Path)),
                    launcher));
                settings.Show();
                settings.UpdateLayout();
                Assert.True(settings.ActualWidth >= settings.MinWidth);
                Assert.True(settings.ActualHeight >= settings.MinHeight);
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
