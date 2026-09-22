// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Serilog;
using Serilog.Events;
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
                .MinimumLevel.Is(ResolveMinimumLevel())
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

        /// <summary>
        /// The panel log (<c>MainWindow.UiLog</c>) writes at <c>Debug</c> while
        /// the file sink's floor is <c>Information</c>, so panel content never
        /// reached <c>runtime/app_*.log</c> -- and there was no environment
        /// override, which left offline replay as the only way to read it.
        /// </summary>
        /// <remarks>
        /// <c>SMSWORKBENCH_LOG_LEVEL</c> lowers (or raises) that floor.  Any
        /// <see cref="LogEventLevel"/> name is accepted
        /// (<c>Verbose</c>/<c>Debug</c>/<c>Information</c>/<c>Warning</c>/
        /// <c>Error</c>/<c>Fatal</c>); <c>Debug</c> is what persists the panel.
        /// Unset, blank or unparsable keeps the previous <c>Information</c>
        /// behaviour, so the default is unchanged.
        /// </remarks>
        public static LogEventLevel ResolveMinimumLevel()
        {
            var raw = Environment.GetEnvironmentVariable("SMSWORKBENCH_LOG_LEVEL");
            if (string.IsNullOrWhiteSpace(raw))
                return LogEventLevel.Information;
            return Enum.TryParse<LogEventLevel>(raw.Trim(), ignoreCase: true, out var level)
                ? level
                : LogEventLevel.Information;
        }
    }
}
