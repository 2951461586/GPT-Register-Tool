namespace SmsWorkbench.Views
{
    public partial class MainWindow
    {
        private readonly ThemeCoordinator themeCoordinator;

        /// <summary>Tooltip for the sidebar theme button: the mode, not the palette.</summary>
        public string ThemeModeLabel => themeCoordinator.CurrentModeLabel;

        private void ToggleTheme()
        {
            DesktopThemeMode next = themeCoordinator.NextMode();
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

            themeCoordinator.PersistMode(rootDir, next, Log);
        }

        public void ApplyStartupTheme(DesktopThemeMode mode, bool log = true)
        {
            ApplyThemeMode(mode);
            if (log) Log($"启动主题已应用: {mode}");
        }

        private void ApplyThemeMode(DesktopThemeMode mode)
        {
            _themeMode = themeCoordinator.SetMode(mode);
            _currentTheme = DesktopThemePreference.Resolve(mode, mode.Resolve());

            try
            {
                Wpf.Ui.Appearance.ApplicationThemeManager.Apply(
                    _currentTheme,
                    Wpf.Ui.Controls.WindowBackdropType.Mica,
                    true);
            }
            catch (Exception ex)
            {
                Log($"Wpf.Ui 主题应用失败: {ex.Message}");
            }

            ApplyCustomThemeColors(_currentTheme);
            WindowThemeService.ApplyToOpenWindows();
            ThemeIconGeometry = IconForMode(mode);
            UpdateSystemThemeWatcher();
            OnPropertyChanged(nameof(ThemeModeLabel));
        }

        private void UpdateSystemThemeWatcher()
        {
            bool shouldWatch = _themeMode == DesktopThemeMode.System;
            if (shouldWatch == systemThemeWatcherArmed) return;

            try
            {
                if (shouldWatch)
                {
                    Wpf.Ui.Appearance.SystemThemeWatcher.Watch(
                        this,
                        Wpf.Ui.Controls.WindowBackdropType.Mica,
                        true);
                }
                else if (IsLoaded)
                {
                    Wpf.Ui.Appearance.SystemThemeWatcher.UnWatch(this);
                }

                systemThemeWatcherArmed = shouldWatch;
            }
            catch (Exception ex)
            {
                systemThemeWatcherArmed = false;
                Log($"系统主题跟随不可用: {ex.Message}");
            }
        }

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

        private Geometry IconForMode(DesktopThemeMode mode) => mode switch
        {
            DesktopThemeMode.Light => sunIcon,
            DesktopThemeMode.Dark => moonIcon,
            _ => systemIcon,
        };

        private static void ApplyCustomThemeColors(Wpf.Ui.Appearance.ApplicationTheme theme)
            => SwapColorScheme(theme);

        private static void SwapColorScheme(Wpf.Ui.Appearance.ApplicationTheme theme)
        {
            if (Application.Current is null) return;

            string wanted = ThemeCoordinator.WantedSchemeFileName(
                theme == Wpf.Ui.Appearance.ApplicationTheme.Dark);
            var merged = Application.Current.Resources.MergedDictionaries;

            for (int index = 0; index < merged.Count; index++)
            {
                string? source = merged[index].Source?.OriginalString;
                if (!ThemeCoordinator.IsColorSchemeSource(source))
                {
                    continue;
                }
                if (source!.EndsWith(wanted, StringComparison.OrdinalIgnoreCase)) return;
                merged[index] = new ResourceDictionary { Source = SchemeUri(wanted) };
                return;
            }

            merged.Add(new ResourceDictionary { Source = SchemeUri(wanted) });
        }

        private static Uri SchemeUri(string fileName)
        {
            string assembly = typeof(MainWindow).Assembly.GetName().Name ?? "SmsWorkbench";
            return ThemeCoordinator.BuildSchemePackUri(assembly, fileName);
        }
    }
}
