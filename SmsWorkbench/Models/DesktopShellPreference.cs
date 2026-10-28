// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>Which shell conveniences this install wants.</summary>
    /// <param name="Tray">Keep running in the notification area when the window closes.</param>
    /// <param name="Hotkey">Register the process-wide show/hide hotkey.</param>
    public sealed record DesktopShellPreferences(bool Tray, bool Hotkey)
    {
        /// <summary>Both on: a fresh install is recoverable after a stray click on the X.</summary>
        public static DesktopShellPreferences Default { get; } = new(Tray: true, Hotkey: true);
    }

    /// <summary>
    /// The persisted shell preferences (<c>runtime/desktop_shell.json</c>).
    /// </summary>
    /// <remarks>
    /// <para>
    /// Separate from the backend configuration on purpose: the config shards are
    /// Python-owned (<c>sms_tool/config</c>) and these two switches describe the WPF
    /// shell's own behaviour. Mixing them would make a desktop toggle a config migration.
    /// </para>
    /// <para>
    /// Both default to on, and a missing or unparseable file reads as the defaults:
    /// an install that loses this file must still be able to hide to the tray, and a
    /// half-written file must not silently disable the only way back into a hidden
    /// window.
    /// </para>
    /// </remarks>
    internal static class DesktopShellPreference
    {
        internal static string StoragePath(string rootDirectory)
            => Path.Combine(rootDirectory, "runtime", "desktop_shell.json");

        public static DesktopShellPreferences Load(string rootDirectory)
        {
            try
            {
                string json = File.ReadAllText(StoragePath(rootDirectory), Encoding.UTF8);
                using JsonDocument document = JsonDocument.Parse(json);
                JsonElement root = document.RootElement;
                if (root.ValueKind != JsonValueKind.Object) return DesktopShellPreferences.Default;

                return new DesktopShellPreferences(
                    Tray: ReadFlag(root, "tray", fallback: DesktopShellPreferences.Default.Tray),
                    Hotkey: ReadFlag(root, "hotkey", fallback: DesktopShellPreferences.Default.Hotkey));
            }
            catch (IOException)
            {
                return DesktopShellPreferences.Default;
            }
            catch (UnauthorizedAccessException)
            {
                return DesktopShellPreferences.Default;
            }
            catch (JsonException)
            {
                return DesktopShellPreferences.Default;
            }
        }

        /// <summary>Persists atomically, like the theme/language preferences.</summary>
        public static void Save(string rootDirectory, DesktopShellPreferences preferences)
        {
            string directory = Path.Combine(rootDirectory, "runtime");
            Directory.CreateDirectory(directory);
            string path = StoragePath(rootDirectory);
            string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                var payload = new Dictionary<string, object?>
                {
                    ["tray"] = preferences.Tray,
                    ["hotkey"] = preferences.Hotkey,
                };
                File.WriteAllText(
                    temporary,
                    JsonSerializer.Serialize(payload, new JsonSerializerOptions { WriteIndented = true }),
                    new UTF8Encoding(false));
                File.Move(temporary, path, overwrite: true);
            }
            finally
            {
                if (File.Exists(temporary))
                    File.Delete(temporary);
            }
        }

        private static bool ReadFlag(JsonElement root, string name, bool fallback)
        {
            if (!root.TryGetProperty(name, out JsonElement value)) return fallback;
            return value.ValueKind switch
            {
                JsonValueKind.True => true,
                JsonValueKind.False => false,
                _ => fallback,
            };
        }
    }
}
