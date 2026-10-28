// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Which palette the shell should use.
    /// </summary>
    /// <remarks>
    /// Three states, not two: <see cref="System"/> is the default and is what most
    /// installs want (the shell looks like the rest of the desktop). It is also the
    /// only state that keeps following the OS after startup — see
    /// <c>SystemThemeWatcher</c> in <c>MainWindow.Theme.cs</c>.
    /// </remarks>
    public enum DesktopThemeMode
    {
        /// <summary>Follow the OS light/dark setting, live.</summary>
        System = 0,

        /// <summary>Always the light palette.</summary>
        Light = 1,

        /// <summary>Always the dark palette.</summary>
        Dark = 2,
    }

    internal static class DesktopThemeModeExtensions
    {
        /// <summary>
        /// The sidebar button's cycle: light -> dark -> system -> light.
        ///
        /// <see cref="DesktopThemeMode.System"/> sits last so that two clicks from
        /// either explicit palette land on the OS-following mode, and the mode the
        /// button reaches first from the default is <see cref="DesktopThemeMode.Light"/>,
        /// which is what "click the sun icon" used to do.
        /// </summary>
        public static DesktopThemeMode Next(this DesktopThemeMode mode) => mode switch
        {
            DesktopThemeMode.Light => DesktopThemeMode.Dark,
            DesktopThemeMode.Dark => DesktopThemeMode.System,
            _ => DesktopThemeMode.Light,
        };

        /// <summary>The token written to <c>runtime/desktop_theme.txt</c>.</summary>
        public static string ToPreferenceText(this DesktopThemeMode mode) => mode switch
        {
            DesktopThemeMode.Light => "Light",
            DesktopThemeMode.Dark => "Dark",
            _ => "System",
        };

        /// <summary>
        /// Parses a preference token. Unrecognised text is not an error: the file is
        /// hand-editable and a name this build does not know (a downgrade, a typo)
        /// must fall back rather than throw during startup.
        /// </summary>
        public static bool TryParsePreference(string? text, out DesktopThemeMode mode)
        {
            switch ((text ?? string.Empty).Trim())
            {
                case "Light":
                case "light":
                    mode = DesktopThemeMode.Light;
                    return true;
                case "Dark":
                case "dark":
                    mode = DesktopThemeMode.Dark;
                    return true;
                case "System":
                case "system":
                    mode = DesktopThemeMode.System;
                    return true;
                default:
                    mode = DesktopThemeMode.System;
                    return false;
            }
        }

        /// <summary>
        /// The palette a mode resolves to. <see cref="DesktopThemeMode.System"/> asks
        /// the OS, and anything that is not one of the OS dark modes counts as light:
        /// high-contrast and the animated themes have no palette of their own here,
        /// and answering <c>Unknown</c> would silently paint the light sheet anyway.
        /// </summary>
        public static Wpf.Ui.Appearance.ApplicationTheme Resolve(this DesktopThemeMode mode)
        {
            switch (mode)
            {
                case DesktopThemeMode.Light:
                    return Wpf.Ui.Appearance.ApplicationTheme.Light;
                case DesktopThemeMode.Dark:
                    return Wpf.Ui.Appearance.ApplicationTheme.Dark;
                default:
                    Wpf.Ui.Appearance.SystemTheme systemTheme =
                        Wpf.Ui.Appearance.ApplicationThemeManager.GetSystemTheme();
                    return systemTheme is Wpf.Ui.Appearance.SystemTheme.Dark
                        or Wpf.Ui.Appearance.SystemTheme.CapturedMotion
                        or Wpf.Ui.Appearance.SystemTheme.Glow
                        ? Wpf.Ui.Appearance.ApplicationTheme.Dark
                        : Wpf.Ui.Appearance.ApplicationTheme.Light;
            }
        }
    }
}
