using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Shapes;
using SmsWorkbench.Localization;

namespace SmsWorkbench.Views
{
    /// <summary>
    /// Sticky utility footer rendered at the bottom of <see cref="Sidebar"/>,
    /// hosting the tweakcn-inspired Appearance Popover (theme polarity, presets,
    /// corner density, live WCAG AA contrast badge, and tweakcn CSS import).
    /// </summary>
    public partial class SidebarFooterBar : UserControl
    {
        private readonly Popup appearancePopup;
        private readonly UniformGrid presetGrid;
        private readonly Button modeSystemBtn;
        private readonly Button modeLightBtn;
        private readonly Button modeDarkBtn;
        private readonly Button radiusSharpBtn;
        private readonly Button radiusDefaultBtn;
        private readonly Button radiusSoftBtn;
        private readonly Border contrastBadge;
        private readonly TextBlock contrastBadgeText;
        private readonly TextBox tweakCnCssBox;
        private readonly TextBlock importStatusText;

        public SidebarFooterBar()
        {
            InitializeComponent();
            appearancePopup = (Popup)FindName("AppearancePopup")!;
            presetGrid = (UniformGrid)FindName("PresetGrid")!;
            modeSystemBtn = (Button)FindName("ModeSystemBtn")!;
            modeLightBtn = (Button)FindName("ModeLightBtn")!;
            modeDarkBtn = (Button)FindName("ModeDarkBtn")!;
            radiusSharpBtn = (Button)FindName("RadiusSharpBtn")!;
            radiusDefaultBtn = (Button)FindName("RadiusDefaultBtn")!;
            radiusSoftBtn = (Button)FindName("RadiusSoftBtn")!;
            contrastBadge = (Border)FindName("ContrastBadge")!;
            contrastBadgeText = (TextBlock)FindName("ContrastBadgeText")!;
            tweakCnCssBox = (TextBox)FindName("TweakCnCssBox")!;
            importStatusText = (TextBlock)FindName("ImportStatusText")!;
        }

        private MainWindow? HostWindow => Window.GetWindow(this) as MainWindow ?? DataContext as MainWindow;

        private void ThemeDockButton_Click(object sender, RoutedEventArgs e)
        {
            appearancePopup.IsOpen = !appearancePopup.IsOpen;
        }

        private void ThemeDockButton_RightClick(object sender, MouseButtonEventArgs e)
        {
            e.Handled = true;
            appearancePopup.IsOpen = false;
            HostWindow?.NavCommand.Execute("theme");
        }

        private void AppearancePopup_Opened(object? sender, EventArgs e)
        {
            RefreshPopoverState();
        }

        private void ModeSystem_Click(object sender, RoutedEventArgs e) => ApplyMode(DesktopThemeMode.System);

        private void ModeLight_Click(object sender, RoutedEventArgs e) => ApplyMode(DesktopThemeMode.Light);

        private void ModeDark_Click(object sender, RoutedEventArgs e) => ApplyMode(DesktopThemeMode.Dark);

        private void RadiusSharp_Click(object sender, RoutedEventArgs e) => ApplyCornerDensity(CornerDensity.Sharp);

        private void RadiusDefault_Click(object sender, RoutedEventArgs e) => ApplyCornerDensity(CornerDensity.Default);

        private void RadiusSoft_Click(object sender, RoutedEventArgs e) => ApplyCornerDensity(CornerDensity.Soft);

        private void ApplyMode(DesktopThemeMode mode)
        {
            HostWindow?.SetThemeMode(mode);
            RefreshPopoverState();
        }

        private void ApplyCornerDensity(CornerDensity density)
        {
            MainWindow? win = HostWindow;
            if (win is null) return;
            win.SetAppearance(win.CurrentAppearance with { CornerDensity = density });
            RefreshPopoverState();
        }

        private void SelectPreset(ThemePresetDefinition preset)
        {
            MainWindow? win = HostWindow;
            if (win is null) return;
            win.SetAppearance(win.CurrentAppearance with { PresetId = preset.Id });
            importStatusText.Visibility = Visibility.Collapsed;
            RefreshPopoverState();
        }

        private void ApplyTweakCnCss_Click(object sender, RoutedEventArgs e)
        {
            MainWindow? win = HostWindow;
            if (win is null) return;

            string css = tweakCnCssBox.Text ?? string.Empty;
            TweakCnParseResult parsed = ThemePresetEngine.TryParseTweakCnCss(css);
            if (!parsed.Ok || parsed.Preset is null)
            {
                ShowImportMessage(parsed.ErrorMessage, isError: true);
                return;
            }

            DesktopAppearancePreferences next = new(
                PresetId: ThemePresetEngine.CustomPresetId,
                CornerDensity: parsed.SuggestedCornerDensity ?? win.CurrentAppearance.CornerDensity,
                CustomTweakCnCss: css);
            win.SetAppearance(next);
            ShowImportMessage(Loc.T("已应用 tweakcn 自定义主题"), isError: false);
            RefreshPopoverState();
        }

        private void ResetAppearance_Click(object sender, RoutedEventArgs e)
        {
            MainWindow? win = HostWindow;
            if (win is null) return;

            tweakCnCssBox.Text = string.Empty;
            win.SetAppearance(new DesktopAppearancePreferences());
            ShowImportMessage(Loc.T("已恢复默认外观"), isError: false);
            RefreshPopoverState();
        }

        private void ShowImportMessage(string message, bool isError)
        {
            importStatusText.Text = message;
            importStatusText.SetResourceReference(
                TextBlock.ForegroundProperty,
                isError ? "Danger" : "Success");
            importStatusText.Visibility = Visibility.Visible;
        }

        private void RefreshPopoverState()
        {
            MainWindow? win = HostWindow;
            if (win is null) return;

            DesktopThemeMode mode = win.CurrentThemeMode;
            DesktopAppearancePreferences pref = win.CurrentAppearance;
            bool isDark = win.IsDarkThemeActive;

            modeSystemBtn.Tag = mode == DesktopThemeMode.System ? "active" : null;
            modeLightBtn.Tag = mode == DesktopThemeMode.Light ? "active" : null;
            modeDarkBtn.Tag = mode == DesktopThemeMode.Dark ? "active" : null;

            radiusSharpBtn.Tag = pref.CornerDensity == CornerDensity.Sharp ? "active" : null;
            radiusDefaultBtn.Tag = pref.CornerDensity == CornerDensity.Default ? "active" : null;
            radiusSoftBtn.Tag = pref.CornerDensity == CornerDensity.Soft ? "active" : null;

            if (string.IsNullOrEmpty(tweakCnCssBox.Text) && !string.IsNullOrWhiteSpace(pref.CustomTweakCnCss))
            {
                tweakCnCssBox.Text = pref.CustomTweakCnCss;
            }

            RebuildPresetCards(pref.PresetId, isDark);

            ThemePresetDefinition activePreset = ThemePresetEngine.ResolvePreset(pref.PresetId, pref.CustomTweakCnCss);
            Dictionary<string, RgbColor> colorMap = ThemePresetEngine.BuildColorMap(
                isDark ? activePreset.Dark : activePreset.Light,
                isDark);
            ThemeContrastReport report = ThemePresetEngine.EvaluateContrast(colorMap);
            contrastBadge.Tag = report.PassesAa ? "success" : "warn";
            contrastBadgeText.Text = report.PassesAa
                ? Loc.F("对比度 AA {0:F1}:1", report.MinimumTextContrast)
                : Loc.F("对比度偏低 {0:F1}:1", report.MinimumTextContrast);
        }

        private void RebuildPresetCards(string activePresetId, bool isDark)
        {
            presetGrid.Children.Clear();
            foreach (ThemePresetDefinition preset in ThemePresetEngine.BuiltInPresets)
            {
                bool isSelected = string.Equals(preset.Id, activePresetId, StringComparison.OrdinalIgnoreCase);
                ThemeSchemeSeed seed = isDark ? preset.Dark : preset.Light;

                var swatchRow = new StackPanel
                {
                    Orientation = Orientation.Horizontal,
                    Margin = new Thickness(0, 0, 0, 5),
                };
                AddDot(swatchRow, seed.SurfacePage);
                AddDot(swatchRow, seed.SurfaceCard);
                AddDot(swatchRow, seed.Primary);
                AddDot(swatchRow, seed.TextMain);

                var title = new TextBlock
                {
                    Text = Loc.T(preset.NameKey),
                    FontWeight = isSelected ? FontWeights.SemiBold : FontWeights.Medium,
                    TextTrimming = TextTrimming.CharacterEllipsis,
                };
                title.SetResourceReference(TextBlock.FontSizeProperty, "Type.Caption.Size");
                title.SetResourceReference(TextBlock.ForegroundProperty, "TextMain");

                var body = new StackPanel();
                body.Children.Add(swatchRow);
                body.Children.Add(title);

                var cardButton = new Button
                {
                    Margin = new Thickness(0, 0, 6, 6),
                    Padding = new Thickness(8, 7, 8, 7),
                    Height = double.NaN,
                    HorizontalContentAlignment = HorizontalAlignment.Stretch,
                    ToolTip = Loc.T(preset.TaglineKey),
                    Content = body,
                };
                cardButton.SetResourceReference(Control.BackgroundProperty, isSelected ? "PrimarySoft" : "Surface.Block");
                cardButton.SetResourceReference(Control.BorderBrushProperty, isSelected ? "AccentIndicator" : "Divider");
                cardButton.Click += (_, _) => SelectPreset(preset);
                presetGrid.Children.Add(cardButton);
            }
        }

        private static void AddDot(StackPanel row, RgbColor rgb)
        {
            var brush = new SolidColorBrush(Color.FromArgb(rgb.A, rgb.R, rgb.G, rgb.B));
            brush.Freeze();
            var dot = new Ellipse
            {
                Width = 10,
                Height = 10,
                Margin = new Thickness(0, 0, 4, 0),
                Fill = brush,
                StrokeThickness = 0.75,
            };
            dot.SetResourceReference(Shape.StrokeProperty, "WorkspaceFrameOutline");
            row.Children.Add(dot);
        }
    }
}
