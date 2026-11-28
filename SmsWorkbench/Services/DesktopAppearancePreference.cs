using System.Text.Json;

namespace SmsWorkbench.Services
{
    /// <summary>
    /// Atomic persistence for preset id, corner density, and optional imported
    /// tweakcn CSS (<c>runtime/desktop_appearance.json</c>). Kept separate from
    /// <c>desktop_theme.txt</c> so the existing light/dark/system contract is
    /// untouched.
    /// </summary>
    public static class DesktopAppearancePreference
    {
        private static readonly JsonSerializerOptions JsonOptions = new()
        {
            WriteIndented = true,
            PropertyNameCaseInsensitive = true,
        };

        private static string FilePath(string rootDirectory)
            => Path.Combine(rootDirectory, "runtime", "desktop_appearance.json");

        public static DesktopAppearancePreferences Load(string rootDirectory)
        {
            try
            {
                string path = FilePath(rootDirectory);
                if (!File.Exists(path)) return new DesktopAppearancePreferences();
                string json = File.ReadAllText(path, Encoding.UTF8);
                DesktopAppearancePreferences? parsed = JsonSerializer.Deserialize<DesktopAppearancePreferences>(json, JsonOptions);
                return parsed ?? new DesktopAppearancePreferences();
            }
            catch
            {
                return new DesktopAppearancePreferences();
            }
        }

        public static void Save(string rootDirectory, DesktopAppearancePreferences preferences)
        {
            string directory = Path.Combine(rootDirectory, "runtime");
            Directory.CreateDirectory(directory);
            string path = FilePath(rootDirectory);
            string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                string json = JsonSerializer.Serialize(preferences, JsonOptions);
                File.WriteAllText(temporary, json, new UTF8Encoding(false));
                File.Move(temporary, path, overwrite: true);
            }
            finally
            {
                if (File.Exists(temporary))
                {
                    File.Delete(temporary);
                }
            }
        }
    }
}
