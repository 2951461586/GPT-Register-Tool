using SmsWorkbench.Localization;

namespace SmsWorkbench.Services
{
    /// <summary>
    /// Window-independent theme mode state, label resolution, pack-URI construction,
    /// and persistence coordinator.
    /// </summary>
    public sealed class ThemeCoordinator
    {
        public const string LightSchemeSource = "Colors.Light.xaml";
        public const string DarkSchemeSource = "Colors.Dark.xaml";

        public DesktopThemeMode CurrentMode { get; private set; } = DesktopThemeMode.System;

        public string CurrentModeLabel => FormatModeLabel(CurrentMode);

        public static string FormatModeLabel(DesktopThemeMode mode) => mode switch
        {
            DesktopThemeMode.Light => Loc.T("主题：浅色"),
            DesktopThemeMode.Dark => Loc.T("主题：深色"),
            _ => Loc.T("主题：跟随系统"),
        };

        public DesktopThemeMode SetMode(DesktopThemeMode mode)
        {
            CurrentMode = mode;
            return CurrentMode;
        }

        public DesktopThemeMode NextMode() => CurrentMode.Next();

        public static string WantedSchemeFileName(bool isDark)
            => isDark ? DarkSchemeSource : LightSchemeSource;

        public static bool IsColorSchemeSource(string? source)
        {
            if (string.IsNullOrEmpty(source))
            {
                return false;
            }
            return source.EndsWith(LightSchemeSource, StringComparison.OrdinalIgnoreCase)
                || source.EndsWith(DarkSchemeSource, StringComparison.OrdinalIgnoreCase);
        }

        public static Uri BuildSchemePackUri(string assemblyName, string fileName)
        {
            string resolvedAssembly = string.IsNullOrWhiteSpace(assemblyName) ? "SmsWorkbench" : assemblyName;
            return new Uri($"pack://application:,,,/{resolvedAssembly};component/Themes/{fileName}", UriKind.Absolute);
        }

        public void PersistMode(string rootDir, DesktopThemeMode mode, Action<string> log)
        {
            try
            {
                DesktopThemePreference.SaveMode(rootDir, mode);
            }
            catch (Exception ex)
            {
                log($"保存主题偏好失败: {ex.Message}");
            }
        }
    }
}
