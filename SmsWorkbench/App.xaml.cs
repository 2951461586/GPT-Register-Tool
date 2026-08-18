using System;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Serilog;
using Wpf.Ui.Appearance;
using Wpf.Ui.Controls;

namespace SmsWorkbench
{
    public partial class App : Application
    {
        private IHost _host;
        private Serilog.ILogger _logger;

        protected override void OnStartup(StartupEventArgs e)
        {
            base.OnStartup(e);
            DispatcherUnhandledException += OnDispatcherUnhandledException;
            AppDomain.CurrentDomain.UnhandledException += OnDomainUnhandledException;
            TaskScheduler.UnobservedTaskException += OnUnobservedTaskException;

            _host = AppHost.Build(AppDomain.CurrentDomain.BaseDirectory);
            _host.Start();
            _logger = _host.Services.GetRequiredService<Serilog.ILogger>();

            var systemTheme = Wpf.Ui.Appearance.ApplicationThemeManager.GetSystemTheme();
            var startTheme = (systemTheme == Wpf.Ui.Appearance.SystemTheme.Dark)
                ? Wpf.Ui.Appearance.ApplicationTheme.Dark
                : Wpf.Ui.Appearance.ApplicationTheme.Light;
            Wpf.Ui.Appearance.ApplicationThemeManager.Apply(startTheme, WindowBackdropType.Mica, true);

            var mainWindow = _host.Services.GetRequiredService<MainWindow>();
            MainWindow = mainWindow;
            mainWindow.Show();
            _ = mainWindow.RunStartupDoctorProbeAsync();
        }

        private void OnDispatcherUnhandledException(object sender, System.Windows.Threading.DispatcherUnhandledExceptionEventArgs e)
        {
            LogCrash(e.Exception);
            System.Windows.MessageBox.Show(e.Exception.Message, "运行异常", System.Windows.MessageBoxButton.OK, System.Windows.MessageBoxImage.Error);
            e.Handled = true;
        }

        private void OnDomainUnhandledException(object sender, UnhandledExceptionEventArgs e)
        {
            if (e.ExceptionObject is Exception ex)
            {
                LogCrash(ex);
            }
        }

        private void OnUnobservedTaskException(object sender, UnobservedTaskExceptionEventArgs e)
        {
            LogCrash(e.Exception);
            e.SetObserved();
        }

        private void LogCrash(Exception ex)
        {
            try
            {
                _logger?.Error(ex, "Unhandled exception");
                // Also write to legacy crash log for backward compatibility
                string dir = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "runtime");
                Directory.CreateDirectory(dir);
                string path = Path.Combine(dir, "ui_errors.log");
                File.AppendAllText(path,
                    "[" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture) + "] " + ex + Environment.NewLine + Environment.NewLine,
                    new UTF8Encoding(false));
            }
            catch
            {
                // best effort only
            }
        }

        protected override void OnExit(ExitEventArgs e)
        {
            try
            {
                // Kill the resident desktop-read python process before the
                // host tears down so it never outlives the workbench.
                (_host?.Services.GetService(typeof(IDesktopReadClient)) as IDisposable)?.Dispose();
                _host?.StopAsync(TimeSpan.FromSeconds(5)).GetAwaiter().GetResult();
                _host?.Dispose();
            }
            finally
            {
                Log.CloseAndFlush();
                base.OnExit(e);
            }
        }
    }
}
