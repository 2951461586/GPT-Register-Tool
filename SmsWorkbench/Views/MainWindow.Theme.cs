// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Theme switching for the desktop shell.
    ///
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
    /// </summary>
    public partial class MainWindow
    {
        private const string LightSchemeSource = "Colors.Light.xaml";
        private const string DarkSchemeSource = "Colors.Dark.xaml";

        // Theme, window chrome and sidebar animation
        private void ToggleTheme()
        {
            _currentTheme = _currentTheme == Wpf.Ui.Appearance.ApplicationTheme.Dark
                ? Wpf.Ui.Appearance.ApplicationTheme.Light
                : Wpf.Ui.Appearance.ApplicationTheme.Dark;

            Log($"切换主题被点击。新主题: {_currentTheme}");

            try
            {
                Wpf.Ui.Appearance.ApplicationThemeManager.Apply(_currentTheme, Wpf.Ui.Controls.WindowBackdropType.Mica, true);
                ApplyCustomThemeColors(_currentTheme);
                // DWM caption colours are per-window native attributes, so they
                // still need one call per window even though the brushes swapped.
                WindowThemeService.ApplyToOpenWindows();
                ThemeIconGeometry = _currentTheme == Wpf.Ui.Appearance.ApplicationTheme.Dark ? MoonIcon : SunIcon;
                Log("主题更新应用成功。");
            }
            catch (Exception ex)
            {
                Log($"应用主题异常: {ex.Message}");
                return;
            }

            try
            {
                DesktopThemePreference.Save(rootDir, _currentTheme);
            }
            catch (Exception ex)
            {
                Log($"保存主题偏好失败: {ex.Message}");
            }
        }

        /// <summary>
        /// Points the shell at the colour scheme for <paramref name="theme"/>.
        ///
        /// Kept as one call site for the constructor and the toggle. The name is
        /// unchanged from the brush-by-brush era so the two callers did not have to
        /// move; the body is now a dictionary swap.
        /// </summary>
        private static void ApplyCustomThemeColors(Wpf.Ui.Appearance.ApplicationTheme theme)
            => SwapColorScheme(theme);

        private static void SwapColorScheme(Wpf.Ui.Appearance.ApplicationTheme theme)
        {
            if (Application.Current is null) return;

            string wanted = theme == Wpf.Ui.Appearance.ApplicationTheme.Dark ? DarkSchemeSource : LightSchemeSource;
            var replacement = new ResourceDictionary { Source = SchemeUri(wanted) };
            var merged = Application.Current.Resources.MergedDictionaries;

            for (int index = 0; index < merged.Count; index++)
            {
                string? source = merged[index].Source?.OriginalString;
                if (source is null) continue;
                // EndsWith, not equality: App.xaml declares a relative URI while the
                // replacement below carries a pack URI, and a second toggle has to
                // recognise the dictionary it installed the first time.
                if (source.EndsWith(LightSchemeSource, StringComparison.OrdinalIgnoreCase)
                    || source.EndsWith(DarkSchemeSource, StringComparison.OrdinalIgnoreCase))
                {
                    merged[index] = replacement;
                    return;
                }
            }

            // Reached only if App.xaml's merge list lost its colour dictionary; adding
            // it back is better than silently rendering with WPF's default brushes.
            merged.Add(replacement);
        }

        /// <summary>
        /// A pack URI that names this assembly explicitly.
        ///
        /// A relative <c>Themes/Colors.Dark.xaml</c> resolves against the entry
        /// assembly, which is the WPF host in production but the *test* host under
        /// <c>dotnet test</c> — there it throws "resource not found". Naming the
        /// assembly keeps one code path working in both.
        /// </summary>
        private static Uri SchemeUri(string fileName)
        {
            string assembly = typeof(MainWindow).Assembly.GetName().Name ?? "SmsWorkbench";
            return new Uri($"pack://application:,,,/{assembly};component/Themes/{fileName}", UriKind.Absolute);
        }
    }
}
