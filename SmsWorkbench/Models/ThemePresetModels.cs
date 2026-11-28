namespace SmsWorkbench.Models
{
    /// <summary>
    /// Global corner-radius density inspired by tweakcn's <c>--radius</c> token.
    /// Every rung preserves concentric nesting (inner = outer - inset) and stays
    /// within WPF's half-short-edge rule so narrow pills never distort into lenses.
    /// </summary>
    public enum CornerDensity
    {
        Sharp = 0,
        Default = 1,
        Soft = 2,
    }

    /// <summary>
    /// Window-independent sRGB color value used by <c>ThemePresetEngine</c> and
    /// <c>TweakCnCssParser</c> so color math and WCAG contrast checks stay testable
    /// without WPF or an STA thread.
    /// </summary>
    public readonly record struct RgbColor(byte R, byte G, byte B, byte A = 255)
    {
        public string ToHex() => A == 255
            ? $"#{R:X2}{G:X2}{B:X2}"
            : $"#{A:X2}{R:X2}{G:X2}{B:X2}";

        public RgbColor WithAlpha(byte alpha) => new(R, G, B, alpha);

        public static RgbColor FromHex(string hex)
        {
            string raw = (hex ?? string.Empty).Trim().TrimStart('#');
            if (raw.Length == 3)
            {
                raw = $"{raw[0]}{raw[0]}{raw[1]}{raw[1]}{raw[2]}{raw[2]}";
            }
            else if (raw.Length == 4)
            {
                raw = $"{raw[0]}{raw[0]}{raw[1]}{raw[1]}{raw[2]}{raw[2]}{raw[3]}{raw[3]}";
            }

            if (raw.Length == 6)
            {
                return new RgbColor(
                    Convert.ToByte(raw[..2], 16),
                    Convert.ToByte(raw[2..4], 16),
                    Convert.ToByte(raw[4..6], 16));
            }

            if (raw.Length == 8)
            {
                return new RgbColor(
                    Convert.ToByte(raw[2..4], 16),
                    Convert.ToByte(raw[4..6], 16),
                    Convert.ToByte(raw[6..8], 16),
                    Convert.ToByte(raw[..2], 16));
            }

            throw new FormatException($"Unsupported hex color '{hex}'.");
        }
    }

    /// <summary>
    /// Core semantic seed tokens for one theme polarity (Light or Dark), mirroring
    /// tweakcn/shadcn's semantic surface + brand + feedback model.
    /// </summary>
    public sealed record ThemeSchemeSeed(
        RgbColor SurfacePage,
        RgbColor SurfaceCard,
        RgbColor SurfaceBlock,
        RgbColor SurfaceInset,
        RgbColor SurfaceChrome,
        RgbColor SurfaceTerminal,
        RgbColor TextMain,
        RgbColor TextSub,
        RgbColor TextMuted,
        RgbColor IconMuted,
        RgbColor Primary,
        RgbColor OnPrimary,
        RgbColor Danger,
        RgbColor OnDanger,
        RgbColor Success,
        RgbColor Warn,
        RgbColor Info,
        RgbColor Line,
        RgbColor LineStrong);

    /// <summary>
    /// Named visual preset combining light and dark semantic seeds with preview
    /// swatches for the Appearance Popover.
    /// </summary>
    public sealed record ThemePresetDefinition(
        string Id,
        string NameKey,
        string TaglineKey,
        ThemeSchemeSeed Light,
        ThemeSchemeSeed Dark,
        CornerDensity DefaultCornerDensity = CornerDensity.Default);

    /// <summary>
    /// Persisted appearance preferences beyond light/dark/system mode
    /// (<c>runtime/desktop_appearance.json</c>).
    /// </summary>
    public sealed record DesktopAppearancePreferences(
        string PresetId = "default",
        CornerDensity CornerDensity = CornerDensity.Default,
        string CustomTweakCnCss = "");

    /// <summary>
    /// Automated WCAG 2.1 AA contrast audit summary for a resolved palette.
    /// </summary>
    public sealed record ThemeContrastReport(
        double MinimumTextContrast,
        bool PassesAa,
        IReadOnlyList<string> Warnings);

    /// <summary>
    /// Outcome of importing a tweakcn CSS theme snippet (<c>:root</c> / <c>.dark</c>).
    /// </summary>
    public sealed record TweakCnParseResult(
        bool Ok,
        ThemePresetDefinition? Preset,
        CornerDensity? SuggestedCornerDensity,
        string ErrorMessage);
}
