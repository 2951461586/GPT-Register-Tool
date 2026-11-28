using System.Windows.Media;

namespace SmsWorkbench.Services
{
    /// <summary>
    /// Applies a resolved <see cref="ThemePresetDefinition"/> and
    /// <see cref="CornerDensity"/> into <see cref="Application.Current"/>'s merged
    /// resource dictionaries without disturbing <c>App.xaml</c>'s load-bearing
    /// <c>Wpf.Ui -> Theme -> Tokens -> Glyphs -> Colors.*</c> ordering.
    /// </summary>
    internal static class ThemeResourceApplier
    {
        private const string PresetMarkerKey = "__SmsWorkbench.PresetOverlay";
        private const string RadiusMarkerKey = "__SmsWorkbench.RadiusOverlay";

        public static void ApplyPresetOverlay(ThemePresetDefinition preset, bool isDark)
        {
            if (Application.Current is null) return;
            var merged = Application.Current.Resources.MergedDictionaries;

            RemoveMarked(merged, PresetMarkerKey);
            if (string.Equals(preset.Id, ThemePresetEngine.DefaultPresetId, StringComparison.OrdinalIgnoreCase))
            {
                return;
            }

            ThemeSchemeSeed seed = isDark ? preset.Dark : preset.Light;
            Dictionary<string, RgbColor> map = ThemePresetEngine.BuildColorMap(seed, isDark);

            var overlay = new ResourceDictionary { [PresetMarkerKey] = preset.Id };
            foreach ((string key, RgbColor rgb) in map)
            {
                Color wpfColor = Color.FromArgb(rgb.A, rgb.R, rgb.G, rgb.B);
                if (string.Equals(key, "ShadowColor", StringComparison.Ordinal))
                {
                    overlay[key] = wpfColor;
                    continue;
                }

                var brush = new SolidColorBrush(wpfColor);
                brush.Freeze();
                overlay[key] = brush;
            }

            int schemeIndex = FindColorSchemeIndex(merged);
            if (schemeIndex >= 0 && schemeIndex + 1 <= merged.Count)
            {
                merged.Insert(schemeIndex + 1, overlay);
            }
            else
            {
                merged.Add(overlay);
            }
        }

        public static void ApplyCornerDensity(CornerDensity density)
        {
            if (Application.Current is null) return;
            var merged = Application.Current.Resources.MergedDictionaries;

            RemoveMarked(merged, RadiusMarkerKey);
            if (density == CornerDensity.Default)
            {
                return;
            }

            (double xs, double sm, double md, double lg, double xl) = density switch
            {
                CornerDensity.Sharp => (2.0, 3.0, 4.0, 6.0, 8.0),
                CornerDensity.Soft => (6.0, 8.0, 10.0, 13.0, 16.0),
                _ => (4.0, 6.0, 8.0, 10.0, 12.0),
            };

            var overlay = new ResourceDictionary
            {
                [RadiusMarkerKey] = density.ToString(),
                ["Corner.Xs"] = new CornerRadius(xs),
                ["Corner.Sm"] = new CornerRadius(sm),
                ["Corner.Md"] = new CornerRadius(md),
                ["Corner.Lg"] = new CornerRadius(lg),
                ["Corner.Xl"] = new CornerRadius(xl),
                ["Radius.Xs"] = xs,
                ["Radius.Sm"] = sm,
                ["Radius.Md"] = md,
            };

            int tokensIndex = FindTokensIndex(merged);
            if (tokensIndex >= 0 && tokensIndex + 1 <= merged.Count)
            {
                merged.Insert(tokensIndex + 1, overlay);
            }
            else
            {
                merged.Add(overlay);
            }
        }

        private static void RemoveMarked(IList<ResourceDictionary> merged, string markerKey)
        {
            for (int i = merged.Count - 1; i >= 0; i--)
            {
                if (merged[i].Contains(markerKey))
                {
                    merged.RemoveAt(i);
                }
            }
        }

        private static int FindColorSchemeIndex(IList<ResourceDictionary> merged)
        {
            for (int i = 0; i < merged.Count; i++)
            {
                if (ThemeCoordinator.IsColorSchemeSource(merged[i].Source?.OriginalString))
                {
                    return i;
                }
            }
            return -1;
        }

        private static int FindTokensIndex(IList<ResourceDictionary> merged)
        {
            for (int i = 0; i < merged.Count; i++)
            {
                string? source = merged[i].Source?.OriginalString;
                if (!string.IsNullOrEmpty(source)
                    && source.EndsWith("Tokens.xaml", StringComparison.OrdinalIgnoreCase))
                {
                    return i;
                }
            }
            return -1;
        }
    }
}
