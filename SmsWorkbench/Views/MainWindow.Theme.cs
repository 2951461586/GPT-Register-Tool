// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Theme switching for the desktop shell: three modes, two dictionary pairs.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The palette used to be written twice: once as XAML (<c>Themes/Palette.xaml</c>)
    /// and once as ~120 <c>SetBrush("#hex")</c> calls in this file, which also had
    /// to be re-applied to every open window through
    /// <see cref="WindowThemeService.ApplyToOpenWindows"/> because a brush written
    /// into <c>Application.Current.Resources</c> does not replace one a window had
    /// already resolved. Since 2026-10-28 the two themes are two dictionaries with
    /// the same key set (<c>Themes/Colors.Light.xaml</c> / <c>Themes/Colors.Dark.xaml</c>)
    /// and switching swaps the merged dictionary as a unit: every control style in
    /// <c>Themes/Controls.xaml</c> reads its colour through <c>DynamicResource</c>,
    /// so open windows re-resolve without being touched.
    /// </para>
    /// <para>
    /// Wpf.Ui keeps its own pair (<c>Resources/Theme/Light.xaml</c> /
    /// <c>Resources/Theme/Dark.xaml</c>) and swaps it through
    /// <c>ApplicationThemeManager.Apply</c>. App.xaml merges that pair for a reason
    /// spelled out there: without it <c>Apply</c> is a no-op and
    /// <c>GetAppTheme()</c> answers Unknown forever.
    /// </para>
    /// <para>
    /// The mode (not the palette) is persisted, and <see cref="DesktopThemeMode.System"/>
    /// keeps following the OS through <c>SystemThemeWatcher</c>: the watcher re-applies
    /// the Wpf.Ui half on a settings change, and <see cref="OnApplicationThemeChanged"/>
    /// brings this side's palette along.
    /// </para>
    /// </remarks>
    public partial class MainWindow
    {
        private const string LightSchemeSource = "Colors.Light.xaml";
        private const string DarkSchemeSource = "Colors.Dark.xaml";

        /// <summary>Tooltip for the sidebar theme button: the mode, not the palette.</summary>
        public string ThemeModeLabel => _themeMode switch
        {
            DesktopThemeMode.Light => SmsWorkbench.Localization.Loc.T("主题：浅色"),
            DesktopThemeMode.Dark => SmsWorkbench.Localization.Loc.T("主题：深色"),
            _ => SmsWorkbench.Localization.Loc.T("主题：跟随系统"),
        };

        // Theme, window chrome and sidebar animation
        private void ToggleTheme()
        {
            DesktopThemeMode next = _themeMode.Next();
            Log($"切换主题被点击。新主题: {next}");

            try
            {
                ApplyThemeMode(next);
                Log("主题更新应用成功。");
            }
            catch (Exception ex)
            {
                Log($"应用主题异常: {ex.Message}");
                return;
            }

            try
            {
                DesktopThemePreference.SaveMode(rootDir, next);
            }
            catch (Exception ex)
            {
                Log($"保存主题偏好失败: {ex.Message}");
            }
        }

        /// <summary>
        /// Apply the mode that was persisted for this install.
        /// </summary>
        /// <remarks>
        /// <para>
        /// Called by <c>App.OnStartup</c> before the window is shown, with the mode
        /// read from <c>runtime/desktop_theme.txt</c>. The file is the authority; the
        /// constructor seeds the same mode silently for callers that build a window
        /// without <c>App.OnStartup</c> (the smoke tests).
        /// </para>
        /// <para>
        /// Idempotent: <see cref="SwapColorScheme"/> is a no-op when the wanted sheet
        /// is the one already merged.
        /// </para>
        /// </remarks>
        /// <param name="mode">Mode to install as the active one.</param>
        /// <param name="log">
        /// Whether to write the <c>启动主题已应用</c> line. The constructor seeds with
        /// <c>log: false</c>; <c>App.OnStartup</c> is the single logging call site
        /// because it owns the persisted value. When the constructor also logged, every
        /// start printed two lines: the constructor's guess ("Light", because
        /// <c>GetAppTheme()</c> could not answer) followed by the truth.
        /// <c>tests/workbench/test_startup_theme_contract.py</c> keeps the logging call
        /// site unique.
        /// </param>
        public void ApplyStartupTheme(DesktopThemeMode mode, bool log = true)
        {
            ApplyThemeMode(mode);
            if (log) Log($"启动主题已应用: {mode}");
        }

        /// <summary>
        /// Installs <paramref name="mode"/>: both dictionaries, the icon, the DWM
        /// caption colours, and the system watcher that has to be armed exactly when
        /// the mode is <see cref="DesktopThemeMode.System"/>.
        /// </summary>
        private void ApplyThemeMode(DesktopThemeMode mode)
        {
            _themeMode = mode;
            _currentTheme = DesktopThemePreference.Resolve(mode, mode.Resolve());

            try
            {
                // Wpf.Ui's half: swaps Resources/Theme/Light.xaml -> Dark.xaml (merged by
                // App.xaml) and refreshes the accent keys. This call was a silent no-op
                // before that merge existed. It also runs
                // WindowBackgroundManager.UpdateBackground, which re-applies the backdrop
                // and rewrites the window's own Background; the shell paints AppBg on the
                // root Grid, so the palette still reaches every pixel.
                Wpf.Ui.Appearance.ApplicationThemeManager.Apply(
                    _currentTheme,
                    Wpf.Ui.Controls.WindowBackdropType.Mica,
                    true);
            }
            catch (Exception ex)
            {
                // The workbench palette below is the part that must not fail. A backdrop
                // or accent failure is cosmetic and must not abort startup.
                Log($"Wpf.Ui 主题应用失败: {ex.Message}");
            }

            ApplyCustomThemeColors(_currentTheme);
            // DWM caption colours are per-window native attributes, so they still need
            // one call per window even though the brushes swapped.
            WindowThemeService.ApplyToOpenWindows();
            ThemeIconGeometry = IconForMode(mode);
            UpdateSystemThemeWatcher();
            OnPropertyChanged(nameof(ThemeModeLabel));
        }

        /// <summary>
        /// Arms or disarms OS theme following. Only <see cref="DesktopThemeMode.System"/>
        /// watches: an explicit Light/Dark install must not be repainted because the OS
        /// changed at dusk.
        /// </summary>
        private void UpdateSystemThemeWatcher()
        {
            bool shouldWatch = _themeMode == DesktopThemeMode.System;
            if (shouldWatch == systemThemeWatcherArmed) return;

            try
            {
                if (shouldWatch)
                {
                    // Applies the OS theme now (for the first watched window) and again
                    // on every settings change; Changed drives the workbench palette.
                    Wpf.Ui.Appearance.SystemThemeWatcher.Watch(
                        this,
                        Wpf.Ui.Controls.WindowBackdropType.Mica,
                        true);
                }
                else if (IsLoaded)
                {
                    // UnWatch throws for a window that was never observed, and for one
                    // that has not loaded yet; the catch below is the guard.
                    Wpf.Ui.Appearance.SystemThemeWatcher.UnWatch(this);
                }

                systemThemeWatcherArmed = shouldWatch;
            }
            catch (Exception ex)
            {
                // A session without an interactive shell has nothing to follow. The
                // palette already resolved to the OS theme, so this is cosmetic.
                systemThemeWatcherArmed = false;
                Log($"系统主题跟随不可用: {ex.Message}");
            }
        }

        /// <summary>
        /// Follows a theme change nobody asked this window for - the OS flipping while
        /// the mode is <see cref="DesktopThemeMode.System"/>. Our own
        /// <c>Apply</c> calls land here too and are ignored, because the palette was
        /// installed before the event was raised.
        /// </summary>
        private void OnApplicationThemeChanged(
            Wpf.Ui.Appearance.ApplicationTheme theme,
            System.Windows.Media.Color systemAccent)
        {
            if (_themeMode != DesktopThemeMode.System) return;
            if (_currentTheme == theme) return;

            _currentTheme = theme;
            ApplyCustomThemeColors(theme);
            WindowThemeService.ApplyToOpenWindows();
            Log($"系统主题已切换: {theme}");
        }

        /// <summary>
        /// The sidebar icon shows the active mode: sun, moon, or the "auto" glyph for
        /// OS following (which is a mode, not a palette - a sun icon would be a lie
        /// half of the day).
        /// </summary>
        private Geometry IconForMode(DesktopThemeMode mode) => mode switch
        {
            DesktopThemeMode.Light => sunIcon,
            DesktopThemeMode.Dark => moonIcon,
            _ => systemIcon,
        };

        /// <summary>
        /// Points the shell at the colour scheme for <paramref name="theme"/>.
        ///
        /// Kept as one call site for every mode application. The name is unchanged from
        /// the brush-by-brush era; the body is now a dictionary swap.
        /// </summary>
        private static void ApplyCustomThemeColors(Wpf.Ui.Appearance.ApplicationTheme theme)
            => SwapColorScheme(theme);

        private static void SwapColorScheme(Wpf.Ui.Appearance.ApplicationTheme theme)
        {
            if (Application.Current is null) return;

            string wanted = theme == Wpf.Ui.Appearance.ApplicationTheme.Dark ? DarkSchemeSource : LightSchemeSource;
            var merged = Application.Current.Resources.MergedDictionaries;

            for (int index = 0; index < merged.Count; index++)
            {
                string? source = merged[index].Source?.OriginalString;
                if (source is null) continue;
                // EndsWith, not equality: App.xaml declares a relative URI while the
                // replacement below carries a pack URI, and a second toggle has to
                // recognise the dictionary it installed the first time.
                if (!source.EndsWith(LightSchemeSource, StringComparison.OrdinalIgnoreCase)
                    && !source.EndsWith(DarkSchemeSource, StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }
                // Already the wanted sheet: replacing it would only re-parse ~64
                // brushes. The constructor's silent seed lands here on every start.
                if (source.EndsWith(wanted, StringComparison.OrdinalIgnoreCase)) return;
                merged[index] = new ResourceDictionary { Source = SchemeUri(wanted) };
                return;
            }

            // Reached only if App.xaml's merge list lost its colour dictionary; adding
            // it back is better than silently rendering with WPF's default brushes.
            merged.Add(new ResourceDictionary { Source = SchemeUri(wanted) });
        }

        /// <summary>
        /// A pack URI that names this assembly explicitly.
        ///
        /// A relative <c>Themes/Colors.Dark.xaml</c> resolves against the entry
        /// assembly, which is the WPF host in production but the *test* host under
        /// <c>dotnet test</c> - there it throws "resource not found". Naming the
        /// assembly keeps one code path working in both.
        /// </summary>
        private static Uri SchemeUri(string fileName)
        {
            string assembly = typeof(MainWindow).Assembly.GetName().Name ?? "SmsWorkbench";
            return new Uri($"pack://application:,,,/{assembly};component/Themes/{fileName}", UriKind.Absolute);
        }
    }
}
