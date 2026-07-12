using System.IO;
using System.Text;
using Serilog;

namespace SmsWorkbench
{
    /// <summary>
    /// Central DI container and Serilog logger configuration.
    /// Provides static access for code-behind pages that cannot use constructor injection
    /// (e.g. MainWindow created via XAML).
    /// </summary>
    public static class AppServices
    {
        private static ServiceProvider _provider;

        public static IServiceProvider Provider => _provider;

        public static Serilog.ILogger Logger { get; private set; }

        public static void Configure(string baseDir)
        {
            var logDir = Path.Combine(baseDir, "runtime");
            Directory.CreateDirectory(logDir);
            var logPath = Path.Combine(logDir, "app_.log");

            Logger = new LoggerConfiguration()
                .MinimumLevel.Information()
                .WriteTo.Async(a => a.File(logPath,
                    rollingInterval: RollingInterval.Day,
                    retainedFileCountLimit: 14,
                    encoding: Encoding.UTF8,
                    outputTemplate: "[{Timestamp:yyyy-MM-dd HH:mm:ss}] [{Level:u3}] {Message:lj}{NewLine}{Exception}"))
                .CreateLogger();

            Log.Logger = Logger;

            var services = new ServiceCollection();
            services.AddSingleton(Logger);
            services.AddSingleton<MainWindow>();
            _provider = services.BuildServiceProvider();
        }

        public static T Resolve<T>() where T : class => _provider?.GetService<T>();
    }
}
