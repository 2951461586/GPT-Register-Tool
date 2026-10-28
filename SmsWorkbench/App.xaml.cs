// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using Wpf.Ui.Controls;

namespace SmsWorkbench
{
    public partial class App : Application
    {
        private IHost? _host;
        private Serilog.ILogger? _logger;
        private string _rootDirectory = "";

        protected override void OnStartup(StartupEventArgs e)
        {
            base.OnStartup(e);
            EventManager.RegisterClassHandler(typeof(Window), FrameworkElement.LoadedEvent,
                new RoutedEventHandler((sender, _) => WindowThemeService.Apply((Window)sender)));
            DispatcherUnhandledException += OnDispatcherUnhandledException;
            AppDomain.CurrentDomain.UnhandledException += OnDomainUnhandledException;
            TaskScheduler.UnobservedTaskException += OnUnobservedTaskException;

            _host = AppHost.Build(AppDomain.CurrentDomain.BaseDirectory);
            _host.Start();
            _logger = _host.Services.GetRequiredService<Serilog.ILogger>();

            var systemTheme = Wpf.Ui.Appearance.ApplicationThemeManager.GetSystemTheme();
            var defaultTheme = (systemTheme == Wpf.Ui.Appearance.SystemTheme.Dark)
                ? Wpf.Ui.Appearance.ApplicationTheme.Dark
                : Wpf.Ui.Appearance.ApplicationTheme.Light;
            string rootDirectory = _host.Services.GetRequiredService<IApplicationPaths>().RootDirectory;
            _rootDirectory = rootDirectory;
            // The retired single-account protocol payment window could leave a
            // plaintext access-token file behind when the process was killed.
            // Sweep it before anything can read the directory.
            int staleAccessTokens = TransientArtifactCleanup.DeletePaymentAccessTokenFiles(rootDirectory);
            if (staleAccessTokens > 0)
            {
                _logger?.Warning(
                    "Removed {Count} stale protocol-payment access-token file(s) from runtime/tmp",
                    staleAccessTokens);
            }
            var startTheme = DesktopThemePreference.Load(rootDirectory, defaultTheme);
            // First launch follows the OS UI language; a saved choice wins afterwards.
            SmsWorkbench.Localization.Loc.Instance.SetLanguage(
                DesktopLanguagePreference.Load(rootDirectory, DesktopLanguagePreference.SystemDefault()));
            Wpf.Ui.Appearance.ApplicationThemeManager.Apply(startTheme, WindowBackdropType.Mica, true);

            var mainWindow = _host.Services.GetRequiredService<MainWindow>();
            MainWindow = mainWindow;
            // The window's own palette follows the preference file, not whatever
            // GetAppTheme() reports (it answers Unknown on failure, which used to
            // fall back to the light palette on every start).
            mainWindow.ApplyStartupTheme(startTheme);
            _logger?.Information(
                "启动偏好: theme={Theme} language={Language}",
                startTheme,
                SmsWorkbench.Localization.Loc.Instance.Language);
            mainWindow.Show();
            _ = mainWindow.RunStartupDoctorProbeAsync();
        }

        private void OnDispatcherUnhandledException(object sender, System.Windows.Threading.DispatcherUnhandledExceptionEventArgs e)
        {
            LogCrash(e.Exception);
            System.Windows.MessageBox.Show(SensitiveDataSanitizer.Redact(e.Exception.Message), SmsWorkbench.Localization.Loc.T("运行异常"), System.Windows.MessageBoxButton.OK, System.Windows.MessageBoxImage.Error);
            e.Handled = true;
        }

        private void OnDomainUnhandledException(object sender, UnhandledExceptionEventArgs e)
        {
            if (e.ExceptionObject is Exception ex)
            {
                LogCrash(ex);
            }
        }

        private void OnUnobservedTaskException(object? sender, UnobservedTaskExceptionEventArgs e)
        {
            LogCrash(e.Exception);
            e.SetObserved();
        }

        private void LogCrash(Exception ex)
        {
            try
            {
                string safeException = SensitiveDataSanitizer.Redact(ex.ToString());
                _logger?.Error("Unhandled exception: {ExceptionText}", safeException);
                // Keep the legacy file only as a bootstrap fallback. Once
                // Serilog is available, runtime/app_*.log is the single WPF
                // lifecycle/error destination.
                if (_logger != null)
                    return;
                string dir = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "runtime");
                Directory.CreateDirectory(dir);
                string path = Path.Combine(dir, "ui_errors.log");
                File.AppendAllText(path,
                    "[" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture) + "] " + safeException + Environment.NewLine + Environment.NewLine,
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
                // Backstop for a file that a crash (or an older build that still
                // wrote one) left in runtime/tmp.
                int staleAccessTokens = TransientArtifactCleanup.DeletePaymentAccessTokenFiles(_rootDirectory);
                if (staleAccessTokens > 0)
                {
                    _logger?.Warning(
                        "Removed {Count} protocol-payment access-token file(s) on shutdown",
                        staleAccessTokens);
                }
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
