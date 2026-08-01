namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Theme, window chrome and sidebar animation
        private void ToggleTheme_Click(object sender, RoutedEventArgs e)
        {
            _currentTheme = _currentTheme == Wpf.Ui.Appearance.ApplicationTheme.Dark
                ? Wpf.Ui.Appearance.ApplicationTheme.Light
                : Wpf.Ui.Appearance.ApplicationTheme.Dark;

            Log($"切换主题被点击。新主题: {_currentTheme}");

            try
            {
                Wpf.Ui.Appearance.ApplicationThemeManager.Apply(_currentTheme, Wpf.Ui.Controls.WindowBackdropType.Mica, true);
                ApplyCustomThemeColors(_currentTheme);
                ThemeIconGeometry = _currentTheme == Wpf.Ui.Appearance.ApplicationTheme.Dark ? MoonIcon : SunIcon;
                Log("主题更新应用成功。");
            }
            catch (Exception ex)
            {
                Log($"应用主题异常: {ex.Message}");
            }
        }

        private void ApplyCustomThemeColors(Wpf.Ui.Appearance.ApplicationTheme theme)
        {
            if (theme == Wpf.Ui.Appearance.ApplicationTheme.Dark)
            {
                // Antigravity-like premium Dark Theme (deep navy/charcoal, neon/slate accent)
                SetBrush("AppBg", "#0F1115");
                SetBrush("PanelBg", "#161920");
                SetBrush("PanelBg2", "#1E222B");
                SetBrush("PanelHover", "#242933");
                SetBrush("Line", "#2C313D");
                SetBrush("LineStrong", "#4C5467");
                SetBrush("Primary", "#E9ECEF");
                SetBrush("PrimarySoft", "#1E222B");
                SetBrush("Danger", "#FA5252");
                SetBrush("DangerSoft", "#2B1D1D");
                SetBrush("DangerBorder", "#8C2A2A");
                SetBrush("Success", "#51CF66");
                SetBrush("SuccessSoft", "#1A2E1F");
                SetBrush("SuccessBorder", "#2B6B3A");
                SetBrush("TextMain", "#F1F3F5");
                SetBrush("TextSub", "#A9B2C3");
                SetBrush("TextMuted", "#6C7A93");
                SetBrush("SidebarBg", "#161920");
                SetBrush("SidebarButtonBg", "#161920");
                SetBrush("GridAltBg", "#12141A");
                SetBrush("SplitterBg", "#2C313D");
                SetBrush("StatusBg", "#0F1115");
                SetBrush("LogBg", "#0A0B0E");
                SetBrush("LogBorder", "#1E222B");
                SetBrush("LogText", "#D1D6E0");

                ApplyComboBoxThemeKeys(
                    dropBg: "#161920", dropBorder: "#2C313D", glyph: "#6C7A93",
                    focused: "#4C5467", pointerOver: "#242933",
                    disabledBg: "#1E222B", disabledBorder: "#2C313D", disabledFg: "#6C7A93");
            }
            else
            {
                // Warm Premium Light Theme
                SetBrush("AppBg", "#F7F5F0");           // 247,245,240
                SetBrush("PanelBg", "#F0EEE8");          // 240,238,232
                SetBrush("PanelBg2", "#DDDAD4");         // 221,218,212
                SetBrush("PanelHover", "#E3E1DB");       // 227,225,219
                SetBrush("Line", "#DDDAD4");             // 221,218,212
                SetBrush("LineStrong", "#C5C2BA");
                SetBrush("Primary", "#3E3B36");
                SetBrush("PrimarySoft", "#E3E1DB");      // 227,225,219
                SetBrush("Danger", "#985248");           // 152,82,72  待支付/未获取
                SetBrush("DangerSoft", "#ECE2DC");       // 236,226,220
                SetBrush("DangerBorder", "#C49088");
                SetBrush("Success", "#3E846F");          // 62,132,111  支付完成/已获取
                SetBrush("SuccessSoft", "#E0F3E6");      // 224,243,230
                SetBrush("SuccessBorder", "#8DC5A9");
                SetBrush("TextMain", "#3E3B36");
                SetBrush("TextSub", "#6B6860");
                SetBrush("TextMuted", "#9E9B93");
                SetBrush("SidebarBg", "#F0EEE8");        // 240,238,232
                SetBrush("SidebarButtonBg", "#DDDAD4");  // 221,218,212
                SetBrush("GridAltBg", "#F7F5F0");        // 247,245,240
                SetBrush("SplitterBg", "#DDDAD4");       // 221,218,212
                SetBrush("StatusBg", "#F7F5F0");         // 247,245,240
                SetBrush("LogBg", "#3E3B36");
                SetBrush("LogBorder", "#55524C");
                SetBrush("LogText", "#E3E1DB");

                ApplyComboBoxThemeKeys(
                    dropBg: "#F0EEE8", dropBorder: "#DDDAD4", glyph: "#9E9B93",
                    focused: "#C5C2BA", pointerOver: "#E3E1DB",
                    disabledBg: "#DDDAD4", disabledBorder: "#DDDAD4", disabledFg: "#9E9B93");
            }
        }

        private void ApplyComboBoxThemeKeys(string dropBg, string dropBorder, string glyph,
            string focused, string pointerOver, string disabledBg, string disabledBorder, string disabledFg)
        {
            SetBrush("ComboBoxDropDownBackground", dropBg);
            SetBrush("ComboBoxDropDownBorderBrush", dropBorder);
            SetBrush("ComboBoxDropDownGlyphForeground", glyph);
            SetBrush("ComboBoxBorderBrushFocused", focused);
            SetBrush("ComboBoxBackgroundPointerOver", pointerOver);
            SetBrush("ComboBoxBackgroundDisabled", disabledBg);
            SetBrush("ComboBoxBorderBrushDisabled", disabledBorder);
            SetBrush("ComboBoxForegroundDisabled", disabledFg);
        }

        private void SetBrush(string key, string hexColor)
        {
            var color = (System.Windows.Media.Color)System.Windows.Media.ColorConverter.ConvertFromString(hexColor);
            var brush = new System.Windows.Media.SolidColorBrush(color);
            Application.Current.Resources[key] = brush;
            this.Resources[key] = brush; // Force local window resource update
        }

        private void ToggleSidebar_Click(object sender, RoutedEventArgs e)
        {
            SidebarCollapsed = !SidebarCollapsed;
        }

        // Custom TitleBar button handlers
        private void TitleBar_MouseLeftButtonDown(object sender, System.Windows.Input.MouseButtonEventArgs e)
        {
            if (e.ClickCount == 2)
            {
                // Double-click to toggle maximize/restore
                WindowState = WindowState == WindowState.Maximized
                    ? WindowState.Normal
                    : WindowState.Maximized;
            }
            else
            {
                DragMove();
            }
        }

        private void MinimizeButton_Click(object sender, RoutedEventArgs e)
        {
            WindowState = WindowState.Minimized;
        }

        private void MaximizeButton_Click(object sender, RoutedEventArgs e)
        {
            WindowState = WindowState == WindowState.Maximized
                ? WindowState.Normal
                : WindowState.Maximized;
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }

        private void ApplySidebarCompact(bool compact)
        {
            if (SidebarToggleButton != null)
            {
                SidebarToggleButton.ToolTip = compact ? "展开侧边栏" : "收起侧边栏";
            }

            SidebarToggleGlyph = compact ? "›" : "‹";
            SidebarToggleGeometry = Geometry.Parse(compact
                ? "M9 18l6-6-6-6"
                : "M15 18l-6-6 6-6");

            AnimateSidebar(compact);
        }

        private const double SidebarExpandedWidth = 272;
        private const double SidebarCollapsedWidth = 80;
        private const int SidebarAnimDurationMs = 220;

        private void AnimateSidebar(bool collapse)
        {
            double target = collapse ? SidebarCollapsedWidth : SidebarExpandedWidth;
            double current = SidebarColumn?.Width.Value ?? (collapse ? SidebarExpandedWidth : SidebarCollapsedWidth);

            sidebarAnimStart = current;
            sidebarAnimTarget = target;

            sidebarAnimTimer?.Stop();
            sidebarAnimTimer = null;
            if (sidebarRenderingHandler != null)
            {
                CompositionTarget.Rendering -= sidebarRenderingHandler;
                sidebarRenderingHandler = null;
            }
            sidebarAnimStopwatch = Stopwatch.StartNew();

            if (SidebarHost != null)
            {
                SidebarHost.Margin = collapse ? new Thickness(8, 0, 8, 10) : new Thickness(10, 0, 10, 10);
            }

            sidebarRenderingHandler = (_, __) =>
            {
                double elapsed = sidebarAnimStopwatch?.Elapsed.TotalMilliseconds ?? SidebarAnimDurationMs;
                double t = Math.Min(1.0, elapsed / SidebarAnimDurationMs);
                double eased = t < 0.5
                    ? 4 * t * t * t
                    : 1 - Math.Pow(-2 * t + 2, 3) / 2;
                double value = Math.Round(sidebarAnimStart + (sidebarAnimTarget - sidebarAnimStart) * eased, 2);

                if (SidebarColumn != null)
                {
                    SidebarColumn.Width = new GridLength(value);
                }

                if (t >= 1.0)
                {
                    if (sidebarRenderingHandler != null)
                    {
                        CompositionTarget.Rendering -= sidebarRenderingHandler;
                        sidebarRenderingHandler = null;
                    }
                    sidebarAnimStopwatch?.Stop();
                    sidebarAnimStopwatch = null;
                    if (SidebarColumn != null)
                    {
                        SidebarColumn.Width = new GridLength(sidebarAnimTarget);
                    }

                    // Update margin and layout after animation completes
                    if (SidebarHost != null)
                    {
                        SidebarHost.ClearValue(FrameworkElement.WidthProperty);
                        SidebarHost.Margin = collapse ? new Thickness(8, 0, 8, 10) : new Thickness(10, 0, 10, 10);
                        SidebarHost.HorizontalAlignment = HorizontalAlignment.Stretch;
                    }
                }
            };

            CompositionTarget.Rendering += sidebarRenderingHandler;
        }

        private static IEnumerable<DependencyObject> FindVisualChildren(DependencyObject node)
        {
            if (node == null) yield break;
            int childCount = VisualTreeHelper.GetChildrenCount(node);
            for (int i = 0; i < childCount; i++)
            {
                DependencyObject child = VisualTreeHelper.GetChild(node, i);
                yield return child;
                foreach (DependencyObject grandChild in FindVisualChildren(child))
                {
                    yield return grandChild;
                }
            }
        }
    }
}
