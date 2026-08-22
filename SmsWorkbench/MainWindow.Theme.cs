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
                WindowThemeService.ApplyToOpenWindows();
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
                // Neutral dark workbench palette with a blue action accent.
                SetBrush("AppBg", "#181818");
                SetBrush("PanelBg", "#181818");
                SetBrush("PanelBg2", "#202020");
                SetBrush("PanelHover", "#2A2A2A");
                SetBrush("Line", "#363636");
                SetBrush("LineStrong", "#505050");
                SetBrush("Primary", "#3B82F6");
                SetBrush("PrimarySoft", "#172554");
                SetBrush("Danger", "#FA5252");
                SetBrush("DangerSoft", "#2B1D1D");
                SetBrush("DangerBorder", "#8C2A2A");
                SetBrush("Success", "#51CF66");
                SetBrush("SuccessSoft", "#1A2E1F");
                SetBrush("SuccessBorder", "#2B6B3A");
                SetBrush("TextMain", "#DFDFDF");
                SetBrush("TextSub", "#DFDFDF");
                SetBrush("TextMuted", "#DFDFDF");
                SetBrush("SidebarBg", "#202020");
                SetBrush("SidebarButtonBg", "#202020");
                SetBrush("GridAltBg", "#1E1E1E");
                SetBrush("GridSelectionBg", "#2A2A2A");
                SetBrush("SettingsSelectionBg", "#2A2A2A");
                SetBrush("SettingsSelectionBorder", "#505050");
                SetBrush("SplitterBg", "#363636");
                SetBrush("StatusBg", "#181818");
                SetBrush("LogBg", "#2A2A2A");
                SetBrush("LogBorder", "#444444");
                SetBrush("LogText", "#DFDFDF");
                ApplyBadgeThemeKeys(
                    success: ("#123524", "#DFDFDF", "#286548"),
                    warn: ("#3A2B0B", "#DFDFDF", "#80611A"),
                    danger: ("#3B1717", "#DFDFDF", "#7F2D2D"),
                    info: ("#172554", "#DFDFDF", "#315DA8"),
                    neutral: ("#202020", "#DFDFDF", "#505050"),
                    accents: ("#60A5FA", "#4ADE80", "#FBBF24", "#A78BFA", "#F87171"));

                ApplyComboBoxThemeKeys(
                    dropBg: "#181818", dropBorder: "#363636", glyph: "#DFDFDF",
                    focused: "#505050", pointerOver: "#2A2A2A",
                    disabledBg: "#202020", disabledBorder: "#363636", disabledFg: "#DFDFDF");
            }
            else
            {
                // Neutral light workbench palette.
                SetBrush("AppBg", "#FFFFFF");
                SetBrush("PanelBg", "#FFFFFF");
                SetBrush("PanelBg2", "#F3F3F3");
                SetBrush("PanelHover", "#F3F3F3");
                SetBrush("Line", "#E2E2E2");
                SetBrush("LineStrong", "#C8C8C8");
                SetBrush("Primary", "#2563EB");
                SetBrush("PrimarySoft", "#EAF2FF");
                SetBrush("Danger", "#B42318");
                SetBrush("DangerSoft", "#FEF0EE");
                SetBrush("DangerBorder", "#F0B8B2");
                SetBrush("Success", "#16794C");
                SetBrush("SuccessSoft", "#EAF7F0");
                SetBrush("SuccessBorder", "#9AD8B7");
                SetBrush("TextMain", "#58595C");
                SetBrush("TextSub", "#58595C");
                SetBrush("TextMuted", "#58595C");
                SetBrush("SidebarBg", "#F3F3F3");
                SetBrush("SidebarButtonBg", "#F3F3F3");
                SetBrush("GridAltBg", "#FAFAFA");
                SetBrush("GridSelectionBg", "#C8C8C8");
                SetBrush("SettingsSelectionBg", "#F3F3F3");
                SetBrush("SettingsSelectionBorder", "#C8C8C8");
                SetBrush("SplitterBg", "#E6E6E6");
                SetBrush("StatusBg", "#FFFFFF");
                SetBrush("LogBg", "#2A2A2A");
                SetBrush("LogBorder", "#444444");
                SetBrush("LogText", "#DFDFDF");
                ApplyBadgeThemeKeys(
                    success: ("#EAF7F0", "#58595C", "#9AD8B7"),
                    warn: ("#FFF7E6", "#58595C", "#F2C66D"),
                    danger: ("#FEF0EE", "#58595C", "#F0B8B2"),
                    info: ("#EAF2FF", "#58595C", "#A9C5FF"),
                    neutral: ("#F1F3F5", "#526173", "#CBD5E1"),
                    accents: ("#2563EB", "#16A34A", "#D97706", "#7C3AED", "#DC2626"));

                ApplyComboBoxThemeKeys(
                    dropBg: "#FFFFFF", dropBorder: "#E2E2E2", glyph: "#58595C",
                    focused: "#C8C8C8", pointerOver: "#F3F3F3",
                    disabledBg: "#F3F3F3", disabledBorder: "#E2E2E2", disabledFg: "#58595C");
            }
        }

        private void ApplyBadgeThemeKeys(
            (string bg, string fg, string bd) success,
            (string bg, string fg, string bd) warn,
            (string bg, string fg, string bd) danger,
            (string bg, string fg, string bd) info,
            (string bg, string fg, string bd) neutral,
            (string blue, string green, string orange, string purple, string red) accents)
        {
            SetBrush("BadgeSuccessBg", success.bg);
            SetBrush("BadgeSuccessFg", success.fg);
            SetBrush("BadgeSuccessBd", success.bd);
            SetBrush("BadgeWarnBg", warn.bg);
            SetBrush("BadgeWarnFg", warn.fg);
            SetBrush("BadgeWarnBd", warn.bd);
            SetBrush("BadgeDangerBg", danger.bg);
            SetBrush("BadgeDangerFg", danger.fg);
            SetBrush("BadgeDangerBd", danger.bd);
            SetBrush("BadgeInfoBg", info.bg);
            SetBrush("BadgeInfoFg", info.fg);
            SetBrush("BadgeInfoBd", info.bd);
            SetBrush("BadgeNeutralBg", neutral.bg);
            SetBrush("BadgeNeutralFg", neutral.fg);
            SetBrush("BadgeNeutralBd", neutral.bd);
            SetBrush("AccentBlue", accents.blue);
            SetBrush("AccentGreen", accents.green);
            SetBrush("AccentOrange", accents.orange);
            SetBrush("AccentPurple", accents.purple);
            SetBrush("AccentRed", accents.red);
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

    }
}
