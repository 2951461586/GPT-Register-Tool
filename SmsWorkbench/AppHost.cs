using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Serilog;
using System.IO;
using System.Text;

namespace SmsWorkbench
{
    public static class AppHost
    {
        public static IHost Build(string baseDirectory)
        {
            var paths = new ApplicationPaths(baseDirectory);
            var logDirectory = Path.Combine(paths.RootDirectory, "runtime");
            Directory.CreateDirectory(logDirectory);

            Log.Logger = new LoggerConfiguration()
                .MinimumLevel.Information()
                .WriteTo.Async(sink => sink.File(
                    Path.Combine(logDirectory, "app_.log"),
                    rollingInterval: RollingInterval.Day,
                    retainedFileCountLimit: 14,
                    encoding: Encoding.UTF8,
                    formatProvider: CultureInfo.InvariantCulture,
                    outputTemplate: "[{Timestamp:yyyy-MM-dd HH:mm:ss}] [{Level:u3}] {Message:lj}{NewLine}{Exception}"))
                .CreateLogger();

            return Host.CreateDefaultBuilder()
                .UseSerilog(Log.Logger, dispose: false)
                .ConfigureServices(services =>
                {
                    services.AddSingleton<Serilog.ILogger>(Log.Logger);
                    services.AddSingleton<IApplicationPaths>(paths);
                    services.AddSingleton<IBackendClient, PythonBackendClient>();
                    services.AddSingleton<IBackendTaskCoordinator, BackendTaskCoordinator>();
                    services.AddSingleton<IDesktopReadClient, DesktopReadClient>();
                    services.AddSingleton<Wpf.Ui.ISnackbarService, Wpf.Ui.SnackbarService>();
                    services.AddSingleton<IFileLauncher, FileLauncher>();
                    services.AddSingleton<IStageMatrixStore, JsonlStageMatrixStore>();
                    services.AddSingleton<IPaymentBatchService, PaymentBatchService>();
                    services.AddSingleton<IPaymentBatchDialogService, PaymentBatchDialogService>();
                    services.AddSingleton<IProtocolPaymentService, ProtocolPaymentService>();
                    services.AddSingleton<IProtocolPaymentDialogService, ProtocolPaymentDialogService>();
                    services.AddSingleton<ISettingsService, SettingsService>();
                    services.AddSingleton<ISettingsDialogService, SettingsDialogService>();
                    services.AddSingleton<MainWindow>();
                })
                .Build();
        }
    }
}
