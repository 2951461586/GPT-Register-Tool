using System.Text.RegularExpressions;
using SmsWorkbench.Localization;

namespace SmsWorkbench.Services
{
    /// <summary>
    /// Window-independent design-token theme engine inspired by <c>tweakcn</c>.
    /// Holds curated presets, derives the full 68-key workbench brush contract
    /// (preserving legacy aliases and surface-ladder invariants), validates WCAG AA
    /// contrast, and parses tweakcn CSS exports (Hex / RGB / HSL / OKLCH).
    /// </summary>
    public static class ThemePresetEngine
    {
        public const string DefaultPresetId = "default";
        public const string CustomPresetId = "custom";

        private static readonly Regex BlockRegex = new(
            @"(?<selector>:root|\.dark)\s*\{(?<body>[^}]*)\}",
            RegexOptions.Compiled | RegexOptions.Singleline | RegexOptions.IgnoreCase);

        private static readonly Regex VarRegex = new(
            @"--(?<name>[a-zA-Z0-9_-]+)\s*:\s*(?<value>[^;]+);",
            RegexOptions.Compiled);

        private static readonly Regex NumberRegex = new(
            @"-?\d+(?:\.\d+)?%?",
            RegexOptions.Compiled);

        public static IReadOnlyList<ThemePresetDefinition> BuiltInPresets { get; } = new[]
        {
            BuildObsidianViolet(),
            BuildVercelMinimal(),
            BuildClaudeWarm(),
            BuildCatppuccin(),
            BuildSupabaseEmerald(),
        };

        public static ThemePresetDefinition ResolvePreset(string? presetId, string? customCss = null)
        {
            string id = (presetId ?? DefaultPresetId).Trim().ToLowerInvariant();
            if (id == CustomPresetId && !string.IsNullOrWhiteSpace(customCss))
            {
                TweakCnParseResult parsed = TryParseTweakCnCss(customCss);
                if (parsed.Ok && parsed.Preset is not null)
                {
                    return parsed.Preset;
                }
            }

            foreach (ThemePresetDefinition preset in BuiltInPresets)
            {
                if (string.Equals(preset.Id, id, StringComparison.OrdinalIgnoreCase))
                {
                    return preset;
                }
            }

            return BuiltInPresets[0];
        }

        /// <summary>
        /// Derives the complete workbench color token dictionary (identical key set to
        /// <c>Colors.Light.xaml</c> / <c>Colors.Dark.xaml</c>) from a semantic seed.
        /// </summary>
        public static Dictionary<string, RgbColor> BuildColorMap(ThemeSchemeSeed seed, bool isDark)
        {
            RgbColor primaryHover = isDark
                ? Mix(seed.Primary, new RgbColor(255, 255, 255), 0.14)
                : Mix(seed.Primary, new RgbColor(255, 255, 255), 0.10);
            RgbColor primaryPressed = Mix(seed.Primary, new RgbColor(0, 0, 0), isDark ? 0.16 : 0.18);
            RgbColor primaryBorder = isDark ? primaryHover : seed.Primary;
            RgbColor primarySoft = Mix(seed.SurfaceCard, seed.Primary, isDark ? 0.18 : 0.09);
            RgbColor accentIndicator = EnsureMinContrast(
                isDark ? Mix(seed.Primary, new RgbColor(255, 255, 255), 0.24) : seed.Primary,
                seed.SurfaceCard,
                minContrast: 4.5,
                preferLighter: isDark);

            RgbColor dangerHover = Mix(seed.Danger, new RgbColor(0, 0, 0), 0.12);
            RgbColor dangerPressed = Mix(seed.Danger, new RgbColor(0, 0, 0), 0.25);
            RgbColor dangerSoft = Mix(seed.SurfaceCard, seed.Danger, isDark ? 0.18 : 0.10);
            RgbColor dangerBorder = Mix(seed.SurfaceCard, seed.Danger, isDark ? 0.48 : 0.34);

            RgbColor sidebarHover = isDark
                ? Mix(seed.SurfaceChrome, new RgbColor(255, 255, 255), 0.08)
                : Mix(seed.SurfaceChrome, new RgbColor(0, 0, 0), 0.06);
            RgbColor sidebarPressed = isDark
                ? Mix(seed.SurfaceChrome, new RgbColor(255, 255, 255), 0.14)
                : Mix(seed.SurfaceChrome, new RgbColor(0, 0, 0), 0.11);
            RgbColor winHover = isDark
                ? Mix(seed.SurfaceChrome, new RgbColor(255, 255, 255), 0.09)
                : Mix(seed.SurfaceChrome, new RgbColor(0, 0, 0), 0.07);
            RgbColor winPressed = isDark
                ? Mix(seed.SurfaceChrome, new RgbColor(255, 255, 255), 0.16)
                : Mix(seed.SurfaceChrome, new RgbColor(0, 0, 0), 0.13);

            RgbColor panelHover = isDark
                ? seed.SurfaceBlock
                : Mix(seed.SurfaceCard, seed.SurfaceBlock, 0.72);
            RgbColor workspaceOutline = seed.TextMain.WithAlpha(isDark ? (byte)0x24 : (byte)0x1F);
            RgbColor divider = seed.TextMain.WithAlpha(isDark ? (byte)0x1F : (byte)0x1C);
            RgbColor shadowColor = new(0, 0, 0, isDark ? (byte)0x7A : (byte)0x24);

            RgbColor onPrimary = EnsureReadableOn(seed.OnPrimary, seed.Primary);
            RgbColor onDanger = EnsureReadableOn(seed.OnDanger, seed.Danger);

            (RgbColor bSuccBg, RgbColor bSuccFg) = BuildBadgePair(seed.SurfaceCard, seed.Success, isDark);
            (RgbColor bWarnBg, RgbColor bWarnFg) = BuildBadgePair(seed.SurfaceCard, seed.Warn, isDark);
            (RgbColor bDangBg, RgbColor bDangFg) = BuildBadgePair(seed.SurfaceCard, seed.Danger, isDark);
            (RgbColor bInfoBg, RgbColor bInfoFg) = BuildBadgePair(seed.SurfaceCard, seed.Info, isDark);
            RgbColor bNeutBg = Mix(seed.SurfaceCard, seed.SurfaceBlock, isDark ? 0.65 : 0.75);
            RgbColor bNeutFg = EnsureMinContrast(seed.TextSub, bNeutBg, 5.0, isDark);

            RgbColor transparent = new(0, 0, 0, 0);

            return new Dictionary<string, RgbColor>(StringComparer.Ordinal)
            {
                // Surface ladder
                ["Surface.Page"] = seed.SurfacePage,
                ["Surface.Card"] = seed.SurfaceCard,
                ["Surface.Block"] = seed.SurfaceBlock,
                ["Surface.Inset"] = seed.SurfaceInset,
                ["Surface.Chrome"] = seed.SurfaceChrome,
                ["Surface.Terminal"] = seed.SurfaceTerminal,

                // Outlines & depth
                ["WorkspaceFrameOutline"] = workspaceOutline,
                ["Divider"] = divider,
                ["FocusRing"] = accentIndicator,
                ["ShadowColor"] = shadowColor,

                // Text & icon ladder
                ["TextMain"] = EnsureMinContrast(seed.TextMain, seed.SurfaceCard, 7.0, isDark),
                ["TextSub"] = EnsureMinContrast(seed.TextSub, seed.SurfaceCard, 4.7, isDark),
                ["TextMuted"] = EnsureMinContrast(seed.TextMuted, seed.SurfaceCard, 4.5, isDark),
                ["IconMuted"] = seed.IconMuted,

                // Action accents
                ["Primary"] = seed.Primary,
                ["PrimaryBorder"] = primaryBorder,
                ["PrimaryHover"] = primaryHover,
                ["PrimaryPressed"] = primaryPressed,
                ["PrimarySoft"] = primarySoft,
                ["OnPrimary"] = onPrimary,
                ["AccentIndicator"] = accentIndicator,
                ["Danger"] = seed.Danger,
                ["DangerHover"] = dangerHover,
                ["DangerPressed"] = dangerPressed,
                ["DangerSoft"] = dangerSoft,
                ["OnDanger"] = onDanger,
                ["DangerBorder"] = dangerBorder,
                ["Success"] = seed.Success,
                ["SidebarHover"] = sidebarHover,
                ["SidebarPressed"] = sidebarPressed,
                ["WindowControlHover"] = winHover,
                ["WindowControlPressed"] = winPressed,

                // Legacy aliases (contracted with WindowThemeService & DesktopWindowSmokeTests)
                ["AppBg"] = seed.SurfacePage,
                ["PanelBg"] = seed.SurfaceCard,
                ["PanelBg2"] = seed.SurfaceBlock,
                ["PanelHover"] = panelHover,
                ["Line"] = seed.Line,
                ["LineStrong"] = seed.LineStrong,
                ["SidebarBg"] = seed.SurfaceChrome,
                ["SidebarButtonBg"] = transparent,
                ["GridAltBg"] = seed.SurfaceCard,
                ["GridSelectionBg"] = seed.SurfaceCard,
                ["SettingsSelectionBg"] = isDark ? seed.SurfaceBlock : seed.SurfaceCard,
                ["SettingsSelectionBorder"] = accentIndicator,
                ["SplitterBg"] = seed.Line,
                ["StatusBg"] = seed.SurfacePage,
                ["LogBg"] = seed.SurfaceTerminal,
                ["LogText"] = EnsureMinContrast(seed.TextMain, seed.SurfaceTerminal, 7.0, isDark),

                // Status badges
                ["BadgeSuccessBg"] = bSuccBg,
                ["BadgeSuccessFg"] = bSuccFg,
                ["BadgeSuccessBd"] = transparent,
                ["BadgeWarnBg"] = bWarnBg,
                ["BadgeWarnFg"] = bWarnFg,
                ["BadgeWarnBd"] = transparent,
                ["BadgeDangerBg"] = bDangBg,
                ["BadgeDangerFg"] = bDangFg,
                ["BadgeDangerBd"] = transparent,
                ["BadgeInfoBg"] = bInfoBg,
                ["BadgeInfoFg"] = bInfoFg,
                ["BadgeInfoBd"] = transparent,
                ["BadgeNeutralBg"] = bNeutBg,
                ["BadgeNeutralFg"] = bNeutFg,

                // ComboBox popup keys
                ["ComboBoxDropDownBackground"] = seed.SurfaceCard,
                ["ComboBoxDropDownBorderBrush"] = seed.Line,
                ["ComboBoxDropDownGlyphForeground"] = seed.TextSub,
                ["ComboBoxBorderBrushFocused"] = accentIndicator,
                ["ComboBoxBackgroundPointerOver"] = seed.SurfaceBlock,
                ["ComboBoxBackgroundDisabled"] = Mix(seed.SurfacePage, seed.SurfaceCard, 0.5),
                ["ComboBoxBorderBrushDisabled"] = seed.Line,
                ["ComboBoxForegroundDisabled"] = seed.TextMuted,
            };
        }

        public static ThemeContrastReport EvaluateContrast(IReadOnlyDictionary<string, RgbColor> palette)
        {
            var checks = new (string Label, string FgKey, string BgKey, double Floor)[]
            {
                ("TextMain / Page", "TextMain", "Surface.Page", 4.5),
                ("TextMain / Card", "TextMain", "Surface.Card", 4.5),
                ("TextSub / Card", "TextSub", "Surface.Card", 4.5),
                ("TextMuted / Card", "TextMuted", "Surface.Card", 4.5),
                ("OnPrimary / Primary", "OnPrimary", "Primary", 4.5),
                ("BadgeSuccess", "BadgeSuccessFg", "BadgeSuccessBg", 4.5),
                ("BadgeWarn", "BadgeWarnFg", "BadgeWarnBg", 4.5),
                ("BadgeDanger", "BadgeDangerFg", "BadgeDangerBg", 4.5),
                ("BadgeInfo", "BadgeInfoFg", "BadgeInfoBg", 4.5),
                ("BadgeNeutral", "BadgeNeutralFg", "BadgeNeutralBg", 4.5),
            };

            double minRatio = double.MaxValue;
            var warnings = new List<string>();
            foreach ((string label, string fgKey, string bgKey, double floor) in checks)
            {
                if (!palette.TryGetValue(fgKey, out RgbColor fg) || !palette.TryGetValue(bgKey, out RgbColor bg))
                {
                    continue;
                }

                double ratio = ContrastRatio(fg, bg);
                if (ratio < minRatio) minRatio = ratio;
                if (ratio < floor)
                {
                    warnings.Add($"{label}: {ratio:F2}:1 (< {floor:F1}:1)");
                }
            }

            if (minRatio == double.MaxValue) minRatio = 0;
            return new ThemeContrastReport(minRatio, warnings.Count == 0, warnings);
        }

        /// <summary>
        /// Parses a tweakcn / shadcn CSS snippet (<c>:root</c> and/or <c>.dark</c>)
        /// supporting Hex, <c>rgb()</c>, <c>hsl()</c>, bare HSL triplets, and Tailwind v4
        /// <c>oklch()</c> color syntax.
        /// </summary>
        public static TweakCnParseResult TryParseTweakCnCss(string? css)
        {
            if (string.IsNullOrWhiteSpace(css))
            {
                return new TweakCnParseResult(false, null, null, Loc.T("请先粘贴 tweakcn 导出的 CSS 变量代码。"));
            }

            var lightVars = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            var darkVars = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

            MatchCollection blocks = BlockRegex.Matches(css);
            if (blocks.Count > 0)
            {
                foreach (Match block in blocks)
                {
                    string selector = block.Groups["selector"].Value.Trim();
                    Dictionary<string, string> target = selector.Equals(".dark", StringComparison.OrdinalIgnoreCase)
                        ? darkVars
                        : lightVars;
                    ExtractVars(block.Groups["body"].Value, target);
                }
            }
            else
            {
                ExtractVars(css, lightVars);
            }

            if (lightVars.Count == 0 && darkVars.Count == 0)
            {
                return new TweakCnParseResult(false, null, null, Loc.T("未识别到有效的 CSS 变量（如 --background / --primary）。"));
            }

            ThemePresetDefinition fallback = BuiltInPresets[0];
            ThemeSchemeSeed lightSeed = lightVars.Count > 0
                ? SeedFromCssVars(lightVars, fallback.Light, isDark: false)
                : fallback.Light;
            ThemeSchemeSeed darkSeed = darkVars.Count > 0
                ? SeedFromCssVars(darkVars, fallback.Dark, isDark: true)
                : SynthesizeDarkFromLight(lightSeed, fallback.Dark);

            CornerDensity? suggestedRadius = ParseRadiusDensity(
                lightVars.TryGetValue("radius", out string? lr) ? lr
                : darkVars.TryGetValue("radius", out string? dr) ? dr
                : null);

            var preset = new ThemePresetDefinition(
                CustomPresetId,
                "自定义 (tweakcn)",
                "来自导入的 tweakcn CSS 变量",
                lightSeed,
                darkSeed,
                suggestedRadius ?? CornerDensity.Default);

            return new TweakCnParseResult(true, preset, suggestedRadius, string.Empty);
        }

        public static bool TryParseCssColor(string? raw, out RgbColor color)
        {
            color = default;
            if (string.IsNullOrWhiteSpace(raw)) return false;
            string text = raw.Trim();

            if (text.StartsWith('#'))
            {
                try
                {
                    color = RgbColor.FromHex(text);
                    return true;
                }
                catch
                {
                    return false;
                }
            }

            if (text.StartsWith("oklch(", StringComparison.OrdinalIgnoreCase))
            {
                return TryParseOklch(text, out color);
            }

            if (text.StartsWith("hsl(", StringComparison.OrdinalIgnoreCase)
                || text.StartsWith("hsla(", StringComparison.OrdinalIgnoreCase))
            {
                return TryParseHsl(text, out color);
            }

            if (text.StartsWith("rgb(", StringComparison.OrdinalIgnoreCase)
                || text.StartsWith("rgba(", StringComparison.OrdinalIgnoreCase))
            {
                return TryParseRgb(text, out color);
            }

            // Bare Tailwind v3 / shadcn HSL triplet: "240 10% 3.9%"
            MatchCollection nums = NumberRegex.Matches(text);
            if (nums.Count >= 3 && text.Contains('%', StringComparison.Ordinal))
            {
                return TryParseHsl(text, out color);
            }

            return false;
        }

        public static double ContrastRatio(RgbColor a, RgbColor b)
        {
            double l1 = RelativeLuminance(a);
            double l2 = RelativeLuminance(b);
            double bright = Math.Max(l1, l2);
            double dark = Math.Min(l1, l2);
            return (bright + 0.05) / (dark + 0.05);
        }

        public static double RelativeLuminance(RgbColor c)
        {
            static double Linear(byte channel)
            {
                double s = channel / 255.0;
                return s <= 0.04045 ? s / 12.92 : Math.Pow((s + 0.055) / 1.055, 2.4);
            }

            return 0.2126 * Linear(c.R) + 0.7152 * Linear(c.G) + 0.0722 * Linear(c.B);
        }

        private static void ExtractVars(string body, Dictionary<string, string> target)
        {
            foreach (Match m in VarRegex.Matches(body))
            {
                string key = m.Groups["name"].Value.Trim();
                string val = m.Groups["value"].Value.Trim();
                if (key.Length > 0 && val.Length > 0)
                {
                    target[key] = val;
                }
            }
        }

        private static ThemeSchemeSeed SeedFromCssVars(
            IReadOnlyDictionary<string, string> vars,
            ThemeSchemeSeed fallback,
            bool isDark)
        {
            RgbColor Pick(string key, RgbColor def)
                => vars.TryGetValue(key, out string? v) && TryParseCssColor(v, out RgbColor c) ? c : def;

            RgbColor bg = Pick("background", fallback.SurfacePage);
            RgbColor fg = Pick("foreground", fallback.TextMain);
            RgbColor card = Pick("card", isDark ? Mix(bg, new RgbColor(255, 255, 255), 0.05) : new RgbColor(255, 255, 255));
            RgbColor muted = Pick("muted", isDark ? Mix(card, new RgbColor(255, 255, 255), 0.06) : Mix(card, bg, 0.6));
            RgbColor sidebar = Pick("sidebar", Pick("sidebar-background", isDark ? Mix(bg, new RgbColor(255, 255, 255), 0.07) : Mix(bg, new RgbColor(0, 0, 0), 0.04)));

            // Keep surface ladder monotonic (dark climbs, light alternates paper & neutral)
            RgbColor surfacePage = bg;
            RgbColor surfaceCard = card;
            if (isDark && RelativeLuminance(surfaceCard) <= RelativeLuminance(surfacePage) + 0.002)
            {
                surfaceCard = Mix(surfacePage, new RgbColor(255, 255, 255), 0.055);
            }
            else if (!isDark && RelativeLuminance(surfaceCard) <= RelativeLuminance(surfacePage) + 0.002)
            {
                surfacePage = Mix(surfaceCard, new RgbColor(0, 0, 0), 0.035);
            }

            RgbColor surfaceBlock = muted;
            if (isDark && RelativeLuminance(surfaceBlock) <= RelativeLuminance(surfaceCard) + 0.002)
            {
                surfaceBlock = Mix(surfaceCard, new RgbColor(255, 255, 255), 0.06);
            }
            else if (!isDark && RelativeLuminance(surfaceBlock) >= RelativeLuminance(surfaceCard) - 0.002)
            {
                surfaceBlock = Mix(surfaceCard, new RgbColor(0, 0, 0), 0.05);
            }

            RgbColor surfaceInset = isDark
                ? Mix(surfaceBlock, new RgbColor(255, 255, 255), 0.06)
                : Mix(surfaceBlock, new RgbColor(0, 0, 0), 0.05);

            RgbColor surfaceChrome = sidebar;
            if (isDark && RelativeLuminance(surfaceChrome) <= RelativeLuminance(surfacePage) + 0.002)
            {
                surfaceChrome = Mix(surfacePage, new RgbColor(255, 255, 255), 0.065);
            }
            else if (!isDark && RelativeLuminance(surfaceChrome) >= RelativeLuminance(surfacePage) - 0.002)
            {
                surfaceChrome = Mix(surfacePage, new RgbColor(0, 0, 0), 0.045);
            }

            RgbColor surfaceTerminal = isDark
                ? Mix(surfacePage, new RgbColor(0, 0, 0), 0.25)
                : Mix(surfaceCard, surfacePage, 0.35);

            RgbColor mutedFg = Pick("muted-foreground", fallback.TextMuted);
            RgbColor textSub = Mix(fg, mutedFg, 0.55);
            RgbColor iconMuted = Mix(mutedFg, surfaceCard, 0.12);

            RgbColor primary = Pick("primary", fallback.Primary);
            RgbColor onPrimary = Pick("primary-foreground", fallback.OnPrimary);
            RgbColor danger = Pick("destructive", fallback.Danger);
            RgbColor onDanger = Pick("destructive-foreground", fallback.OnDanger);
            RgbColor border = Pick("border", fallback.Line);
            RgbColor borderStrong = isDark
                ? Mix(border, new RgbColor(255, 255, 255), 0.14)
                : Mix(border, new RgbColor(0, 0, 0), 0.12);

            return new ThemeSchemeSeed(
                SurfacePage: surfacePage,
                SurfaceCard: surfaceCard,
                SurfaceBlock: surfaceBlock,
                SurfaceInset: surfaceInset,
                SurfaceChrome: surfaceChrome,
                SurfaceTerminal: surfaceTerminal,
                TextMain: fg,
                TextSub: textSub,
                TextMuted: mutedFg,
                IconMuted: iconMuted,
                Primary: primary,
                OnPrimary: onPrimary,
                Danger: danger,
                OnDanger: onDanger,
                Success: fallback.Success,
                Warn: fallback.Warn,
                Info: fallback.Info,
                Line: border,
                LineStrong: borderStrong);
        }

        private static ThemeSchemeSeed SynthesizeDarkFromLight(ThemeSchemeSeed light, ThemeSchemeSeed darkFallback)
            => darkFallback with
            {
                Primary = EnsureMinContrast(light.Primary, darkFallback.SurfaceCard, 3.2, preferLighter: true),
                OnPrimary = EnsureReadableOn(light.OnPrimary, light.Primary),
            };

        private static CornerDensity? ParseRadiusDensity(string? raw)
        {
            if (string.IsNullOrWhiteSpace(raw)) return null;
            Match m = NumberRegex.Match(raw);
            if (!m.Success) return null;
            if (!double.TryParse(m.Value.TrimEnd('%'), NumberStyles.Float, CultureInfo.InvariantCulture, out double value))
                return null;

            double px = raw.Contains("rem", StringComparison.OrdinalIgnoreCase) ? value * 16.0 : value;
            if (px <= 4.5) return CornerDensity.Sharp;
            if (px >= 11.5) return CornerDensity.Soft;
            return CornerDensity.Default;
        }

        private static (RgbColor Bg, RgbColor Fg) BuildBadgePair(RgbColor surfaceCard, RgbColor accent, bool isDark)
        {
            RgbColor bg = Mix(surfaceCard, accent, isDark ? 0.16 : 0.11);
            RgbColor seedFg = isDark
                ? Mix(accent, new RgbColor(245, 245, 247), 0.35)
                : Mix(accent, new RgbColor(24, 24, 28), 0.42);
            RgbColor fg = EnsureMinContrast(seedFg, bg, 5.2, preferLighter: isDark);
            return (bg, fg);
        }

        private static RgbColor EnsureReadableOn(RgbColor preferred, RgbColor background)
        {
            if (ContrastRatio(preferred, background) >= 4.5) return preferred;
            RgbColor white = new(255, 255, 255);
            RgbColor black = new(0, 0, 0);
            return ContrastRatio(white, background) >= ContrastRatio(black, background) ? white : black;
        }

        private static RgbColor EnsureMinContrast(RgbColor fg, RgbColor bg, double minContrast, bool preferLighter)
        {
            if (ContrastRatio(fg, bg) >= minContrast) return fg;
            RgbColor target = preferLighter ? new RgbColor(248, 248, 250) : new RgbColor(18, 18, 22);
            for (int step = 1; step <= 16; step++)
            {
                RgbColor candidate = Mix(fg, target, step / 16.0);
                if (ContrastRatio(candidate, bg) >= minContrast)
                {
                    return candidate;
                }
            }
            return target;
        }

        public static RgbColor Mix(RgbColor a, RgbColor b, double weightB)
        {
            double t = Math.Clamp(weightB, 0.0, 1.0);
            byte r = (byte)Math.Clamp(Math.Round(a.R + (b.R - a.R) * t), 0, 255);
            byte g = (byte)Math.Clamp(Math.Round(a.G + (b.G - a.G) * t), 0, 255);
            byte bl = (byte)Math.Clamp(Math.Round(a.B + (b.B - a.B) * t), 0, 255);
            return new RgbColor(r, g, bl);
        }

        private static bool TryParseRgb(string text, out RgbColor color)
        {
            color = default;
            MatchCollection nums = NumberRegex.Matches(text);
            if (nums.Count < 3) return false;
            if (!TryParseChannel(nums[0].Value, 255, out double r)
                || !TryParseChannel(nums[1].Value, 255, out double g)
                || !TryParseChannel(nums[2].Value, 255, out double b))
                return false;

            color = new RgbColor(ClampByte(r), ClampByte(g), ClampByte(b));
            return true;
        }

        private static bool TryParseHsl(string text, out RgbColor color)
        {
            color = default;
            MatchCollection nums = NumberRegex.Matches(text);
            if (nums.Count < 3) return false;
            if (!double.TryParse(nums[0].Value.TrimEnd('%'), NumberStyles.Float, CultureInfo.InvariantCulture, out double h)
                || !double.TryParse(nums[1].Value.TrimEnd('%'), NumberStyles.Float, CultureInfo.InvariantCulture, out double s)
                || !double.TryParse(nums[2].Value.TrimEnd('%'), NumberStyles.Float, CultureInfo.InvariantCulture, out double l))
                return false;

            h = ((h % 360.0) + 360.0) % 360.0;
            s = Math.Clamp(s / 100.0, 0.0, 1.0);
            l = Math.Clamp(l / 100.0, 0.0, 1.0);

            double c = (1.0 - Math.Abs(2.0 * l - 1.0)) * s;
            double x = c * (1.0 - Math.Abs((h / 60.0) % 2.0 - 1.0));
            double m = l - c / 2.0;

            (double r1, double g1, double b1) = h switch
            {
                < 60 => (c, x, 0.0),
                < 120 => (x, c, 0.0),
                < 180 => (0.0, c, x),
                < 240 => (0.0, x, c),
                < 300 => (x, 0.0, c),
                _ => (c, 0.0, x),
            };

            color = new RgbColor(
                ClampByte((r1 + m) * 255.0),
                ClampByte((g1 + m) * 255.0),
                ClampByte((b1 + m) * 255.0));
            return true;
        }

        private static bool TryParseOklch(string text, out RgbColor color)
        {
            color = default;
            MatchCollection nums = NumberRegex.Matches(text);
            if (nums.Count < 3) return false;
            if (!TryParseChannel(nums[0].Value, 1.0, out double L)
                || !double.TryParse(nums[1].Value.TrimEnd('%'), NumberStyles.Float, CultureInfo.InvariantCulture, out double C)
                || !double.TryParse(nums[2].Value.TrimEnd('%'), NumberStyles.Float, CultureInfo.InvariantCulture, out double H))
                return false;

            double rad = H * Math.PI / 180.0;
            double a = C * Math.Cos(rad);
            double b = C * Math.Sin(rad);

            double l_ = L + 0.3963377774 * a + 0.2158037573 * b;
            double m_ = L - 0.1055613458 * a - 0.0638541728 * b;
            double s_ = L - 0.0894841775 * a - 1.2914855480 * b;

            double l = l_ * l_ * l_;
            double m = m_ * m_ * m_;
            double s = s_ * s_ * s_;

            double lr = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s;
            double lg = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s;
            double lb = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s;

            static byte ToSrgbByte(double lin)
            {
                double clamped = Math.Clamp(lin, 0.0, 1.0);
                double srgb = clamped <= 0.0031308
                    ? 12.92 * clamped
                    : 1.055 * Math.Pow(clamped, 1.0 / 2.4) - 0.055;
                return ClampByte(srgb * 255.0);
            }

            color = new RgbColor(ToSrgbByte(lr), ToSrgbByte(lg), ToSrgbByte(lb));
            return true;
        }

        private static bool TryParseChannel(string raw, double percentBase, out double value)
        {
            bool isPercent = raw.EndsWith('%');
            string cleaned = isPercent ? raw[..^1] : raw;
            if (!double.TryParse(cleaned, NumberStyles.Float, CultureInfo.InvariantCulture, out double parsed))
            {
                value = 0;
                return false;
            }
            value = isPercent ? parsed / 100.0 * percentBase : parsed;
            return true;
        }

        private static byte ClampByte(double v) => (byte)Math.Clamp(Math.Round(v), 0, 255);

        private static ThemePresetDefinition BuildObsidianViolet() => new(
            Id: DefaultPresetId,
            NameKey: "曜石紫罗兰（默认）",
            TaglineKey: "冷静石墨表面与紫罗兰焦点",
            Light: new ThemeSchemeSeed(
                SurfacePage: RgbColor.FromHex("#F5F5F6"),
                SurfaceCard: RgbColor.FromHex("#FFFFFF"),
                SurfaceBlock: RgbColor.FromHex("#F4F4F5"),
                SurfaceInset: RgbColor.FromHex("#EBEBED"),
                SurfaceChrome: RgbColor.FromHex("#EEEEEE"),
                SurfaceTerminal: RgbColor.FromHex("#FAFAFA"),
                TextMain: RgbColor.FromHex("#1E1F22"),
                TextSub: RgbColor.FromHex("#52565E"),
                TextMuted: RgbColor.FromHex("#636871"),
                IconMuted: RgbColor.FromHex("#757A84"),
                Primary: RgbColor.FromHex("#6A20D0"),
                OnPrimary: RgbColor.FromHex("#FFFFFF"),
                Danger: RgbColor.FromHex("#C62828"),
                OnDanger: RgbColor.FromHex("#FFFFFF"),
                Success: RgbColor.FromHex("#166534"),
                Warn: RgbColor.FromHex("#9A6700"),
                Info: RgbColor.FromHex("#2E5AAC"),
                Line: RgbColor.FromHex("#E0E2E6"),
                LineStrong: RgbColor.FromHex("#C9CDD4")),
            Dark: new ThemeSchemeSeed(
                SurfacePage: RgbColor.FromHex("#141416"),
                SurfaceCard: RgbColor.FromHex("#1D1D20"),
                SurfaceBlock: RgbColor.FromHex("#26262A"),
                SurfaceInset: RgbColor.FromHex("#2F2F34"),
                SurfaceChrome: RgbColor.FromHex("#202023"),
                SurfaceTerminal: RgbColor.FromHex("#111113"),
                TextMain: RgbColor.FromHex("#F2F2F4"),
                TextSub: RgbColor.FromHex("#B0B3BA"),
                TextMuted: RgbColor.FromHex("#8A8F99"),
                IconMuted: RgbColor.FromHex("#7E838E"),
                Primary: RgbColor.FromHex("#7C3AED"),
                OnPrimary: RgbColor.FromHex("#FFFFFF"),
                Danger: RgbColor.FromHex("#F05252"),
                OnDanger: RgbColor.FromHex("#FFFFFF"),
                Success: RgbColor.FromHex("#4ADE80"),
                Warn: RgbColor.FromHex("#EAB308"),
                Info: RgbColor.FromHex("#60A5FA"),
                Line: RgbColor.FromHex("#2E2E34"),
                LineStrong: RgbColor.FromHex("#404048")));

        private static ThemePresetDefinition BuildVercelMinimal() => new(
            Id: "vercel",
            NameKey: "Vercel 极简",
            TaglineKey: "高对比纯粹黑白与利落微灰",
            Light: new ThemeSchemeSeed(
                SurfacePage: RgbColor.FromHex("#F6F6F7"),
                SurfaceCard: RgbColor.FromHex("#FFFFFF"),
                SurfaceBlock: RgbColor.FromHex("#F2F2F4"),
                SurfaceInset: RgbColor.FromHex("#E6E6EA"),
                SurfaceChrome: RgbColor.FromHex("#EFEFF1"),
                SurfaceTerminal: RgbColor.FromHex("#FAFAFA"),
                TextMain: RgbColor.FromHex("#111113"),
                TextSub: RgbColor.FromHex("#4C4F57"),
                TextMuted: RgbColor.FromHex("#626670"),
                IconMuted: RgbColor.FromHex("#71757F"),
                Primary: RgbColor.FromHex("#18181B"),
                OnPrimary: RgbColor.FromHex("#FFFFFF"),
                Danger: RgbColor.FromHex("#DC2626"),
                OnDanger: RgbColor.FromHex("#FFFFFF"),
                Success: RgbColor.FromHex("#15803D"),
                Warn: RgbColor.FromHex("#A16207"),
                Info: RgbColor.FromHex("#2563EB"),
                Line: RgbColor.FromHex("#E2E2E7"),
                LineStrong: RgbColor.FromHex("#C8C8D0")),
            Dark: new ThemeSchemeSeed(
                SurfacePage: RgbColor.FromHex("#0A0A0B"),
                SurfaceCard: RgbColor.FromHex("#131316"),
                SurfaceBlock: RgbColor.FromHex("#1C1C21"),
                SurfaceInset: RgbColor.FromHex("#26262C"),
                SurfaceChrome: RgbColor.FromHex("#111113"),
                SurfaceTerminal: RgbColor.FromHex("#070708"),
                TextMain: RgbColor.FromHex("#FAFAFA"),
                TextSub: RgbColor.FromHex("#A8ABB4"),
                TextMuted: RgbColor.FromHex("#848894"),
                IconMuted: RgbColor.FromHex("#767A85"),
                Primary: RgbColor.FromHex("#F4F4F5"),
                OnPrimary: RgbColor.FromHex("#09090B"),
                Danger: RgbColor.FromHex("#F87171"),
                OnDanger: RgbColor.FromHex("#1A0808"),
                Success: RgbColor.FromHex("#4ADE80"),
                Warn: RgbColor.FromHex("#FACC15"),
                Info: RgbColor.FromHex("#60A5FA"),
                Line: RgbColor.FromHex("#27272E"),
                LineStrong: RgbColor.FromHex("#3A3A44")),
            DefaultCornerDensity: CornerDensity.Sharp);

        private static ThemePresetDefinition BuildClaudeWarm() => new(
            Id: "claude",
            NameKey: "Claude 暖陶",
            TaglineKey: "柔和羊皮纸与赤陶暖色系",
            Light: new ThemeSchemeSeed(
                SurfacePage: RgbColor.FromHex("#F6F3EC"),
                SurfaceCard: RgbColor.FromHex("#FCFBF8"),
                SurfaceBlock: RgbColor.FromHex("#EFEAE0"),
                SurfaceInset: RgbColor.FromHex("#E6DFD2"),
                SurfaceChrome: RgbColor.FromHex("#EFECE4"),
                SurfaceTerminal: RgbColor.FromHex("#F9F7F2"),
                TextMain: RgbColor.FromHex("#26221E"),
                TextSub: RgbColor.FromHex("#5C5349"),
                TextMuted: RgbColor.FromHex("#70665B"),
                IconMuted: RgbColor.FromHex("#82776B"),
                Primary: RgbColor.FromHex("#B54B27"),
                OnPrimary: RgbColor.FromHex("#FFFFFF"),
                Danger: RgbColor.FromHex("#B91C1C"),
                OnDanger: RgbColor.FromHex("#FFFFFF"),
                Success: RgbColor.FromHex("#2B6A4A"),
                Warn: RgbColor.FromHex("#9A5B13"),
                Info: RgbColor.FromHex("#355C8C"),
                Line: RgbColor.FromHex("#E2DBCE"),
                LineStrong: RgbColor.FromHex("#CEC4B3")),
            Dark: new ThemeSchemeSeed(
                SurfacePage: RgbColor.FromHex("#191715"),
                SurfaceCard: RgbColor.FromHex("#221F1C"),
                SurfaceBlock: RgbColor.FromHex("#2C2824"),
                SurfaceInset: RgbColor.FromHex("#36312C"),
                SurfaceChrome: RgbColor.FromHex("#1F1C19"),
                SurfaceTerminal: RgbColor.FromHex("#141210"),
                TextMain: RgbColor.FromHex("#F3EFE8"),
                TextSub: RgbColor.FromHex("#BCB4A8"),
                TextMuted: RgbColor.FromHex("#968D80"),
                IconMuted: RgbColor.FromHex("#887F73"),
                Primary: RgbColor.FromHex("#D96B43"),
                OnPrimary: RgbColor.FromHex("#1B100A"),
                Danger: RgbColor.FromHex("#EF6B6B"),
                OnDanger: RgbColor.FromHex("#1F0B0B"),
                Success: RgbColor.FromHex("#6CC28D"),
                Warn: RgbColor.FromHex("#E2B05B"),
                Info: RgbColor.FromHex("#7BA7D9"),
                Line: RgbColor.FromHex("#35302B"),
                LineStrong: RgbColor.FromHex("#48413A")));

        private static ThemePresetDefinition BuildCatppuccin() => new(
            Id: "catppuccin",
            NameKey: "Catppuccin 柔彩",
            TaglineKey: "护眼低疲劳蓝灰与柔紫强调",
            Light: new ThemeSchemeSeed(
                SurfacePage: RgbColor.FromHex("#EFF1F5"),
                SurfaceCard: RgbColor.FromHex("#F9FAFC"),
                SurfaceBlock: RgbColor.FromHex("#E6E9EF"),
                SurfaceInset: RgbColor.FromHex("#DCE0E8"),
                SurfaceChrome: RgbColor.FromHex("#E6E9EF"),
                SurfaceTerminal: RgbColor.FromHex("#F4F6FA"),
                TextMain: RgbColor.FromHex("#2D3047"),
                TextSub: RgbColor.FromHex("#535770"),
                TextMuted: RgbColor.FromHex("#666B85"),
                IconMuted: RgbColor.FromHex("#787E99"),
                Primary: RgbColor.FromHex("#7B35DF"),
                OnPrimary: RgbColor.FromHex("#FFFFFF"),
                Danger: RgbColor.FromHex("#C52243"),
                OnDanger: RgbColor.FromHex("#FFFFFF"),
                Success: RgbColor.FromHex("#27753C"),
                Warn: RgbColor.FromHex("#9B5E0E"),
                Info: RgbColor.FromHex("#1E60B8"),
                Line: RgbColor.FromHex("#D4D8E3"),
                LineStrong: RgbColor.FromHex("#BCC2D1")),
            Dark: new ThemeSchemeSeed(
                SurfacePage: RgbColor.FromHex("#181825"),
                SurfaceCard: RgbColor.FromHex("#1E1E2E"),
                SurfaceBlock: RgbColor.FromHex("#28283D"),
                SurfaceInset: RgbColor.FromHex("#33334C"),
                SurfaceChrome: RgbColor.FromHex("#1B1B2A"),
                SurfaceTerminal: RgbColor.FromHex("#11111B"),
                TextMain: RgbColor.FromHex("#CDD6F4"),
                TextSub: RgbColor.FromHex("#A6ADC8"),
                TextMuted: RgbColor.FromHex("#8890AD"),
                IconMuted: RgbColor.FromHex("#7A829E"),
                Primary: RgbColor.FromHex("#A78BFA"),
                OnPrimary: RgbColor.FromHex("#141223"),
                Danger: RgbColor.FromHex("#F38BA8"),
                OnDanger: RgbColor.FromHex("#230D14"),
                Success: RgbColor.FromHex("#A6E3A1"),
                Warn: RgbColor.FromHex("#F9E2AF"),
                Info: RgbColor.FromHex("#89B4FA"),
                Line: RgbColor.FromHex("#313244"),
                LineStrong: RgbColor.FromHex("#45475A")),
            DefaultCornerDensity: CornerDensity.Soft);

        private static ThemePresetDefinition BuildSupabaseEmerald() => new(
            Id: "supabase",
            NameKey: "Supabase 翡翠",
            TaglineKey: "深炭工程表面与翡翠绿焦点",
            Light: new ThemeSchemeSeed(
                SurfacePage: RgbColor.FromHex("#F4F6F5"),
                SurfaceCard: RgbColor.FromHex("#FFFFFF"),
                SurfaceBlock: RgbColor.FromHex("#EEF1EF"),
                SurfaceInset: RgbColor.FromHex("#E3E8E5"),
                SurfaceChrome: RgbColor.FromHex("#EBEEEC"),
                SurfaceTerminal: RgbColor.FromHex("#F9FBFA"),
                TextMain: RgbColor.FromHex("#181C1A"),
                TextSub: RgbColor.FromHex("#4D5652"),
                TextMuted: RgbColor.FromHex("#606B66"),
                IconMuted: RgbColor.FromHex("#727E78"),
                Primary: RgbColor.FromHex("#0F7A52"),
                OnPrimary: RgbColor.FromHex("#FFFFFF"),
                Danger: RgbColor.FromHex("#C62828"),
                OnDanger: RgbColor.FromHex("#FFFFFF"),
                Success: RgbColor.FromHex("#157347"),
                Warn: RgbColor.FromHex("#9A6700"),
                Info: RgbColor.FromHex("#2563EB"),
                Line: RgbColor.FromHex("#DCE2DF"),
                LineStrong: RgbColor.FromHex("#C2CCC7")),
            Dark: new ThemeSchemeSeed(
                SurfacePage: RgbColor.FromHex("#121413"),
                SurfaceCard: RgbColor.FromHex("#1A1D1B"),
                SurfaceBlock: RgbColor.FromHex("#222724"),
                SurfaceInset: RgbColor.FromHex("#2B312E"),
                SurfaceChrome: RgbColor.FromHex("#161917"),
                SurfaceTerminal: RgbColor.FromHex("#0D0F0E"),
                TextMain: RgbColor.FromHex("#EEF2F0"),
                TextSub: RgbColor.FromHex("#A9B4AE"),
                TextMuted: RgbColor.FromHex("#84918A"),
                IconMuted: RgbColor.FromHex("#77847D"),
                Primary: RgbColor.FromHex("#34C787"),
                OnPrimary: RgbColor.FromHex("#071C12"),
                Danger: RgbColor.FromHex("#F06565"),
                OnDanger: RgbColor.FromHex("#210A0A"),
                Success: RgbColor.FromHex("#4ADE80"),
                Warn: RgbColor.FromHex("#EAB308"),
                Info: RgbColor.FromHex("#60A5FA"),
                Line: RgbColor.FromHex("#2A302D"),
                LineStrong: RgbColor.FromHex("#3B443F")));
    }
}
