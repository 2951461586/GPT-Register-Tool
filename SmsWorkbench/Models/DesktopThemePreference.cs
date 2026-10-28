// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System;
using System.IO;
using System.Text;
using Wpf.Ui.Appearance;

namespace SmsWorkbench;

/// <summary>
/// The persisted theme choice (<c>runtime/desktop_theme.txt</c>).
/// </summary>
/// <remarks>
/// <para>
/// The file holds the operator's <em>mode</em> - <c>System</c>, <c>Light</c> or
/// <c>Dark</c> - not a resolved palette. Storing the resolved value is what would
/// break "follow the system": if the shell wrote <c>Dark</c> the moment the OS went
/// dark, that install would stay dark after the OS went back to light.
/// </para>
/// <para>
/// <see cref="Load(string, ApplicationTheme)"/> and <see cref="Save(string, ApplicationTheme)"/>
/// are the two-state surface older call sites use; they are wrappers so a caller that
/// only knows a palette does not have to learn about the mode enum.
/// </para>
/// </remarks>
internal static class DesktopThemePreference
{
    private static string ThemePath(string rootDirectory)
        => Path.Combine(rootDirectory, "runtime", "desktop_theme.txt");

    /// <summary>
    /// Reads the persisted mode. A missing, unreadable or unrecognised file is
    /// <see cref="DesktopThemeMode.System"/>: following the OS is the safer default
    /// than pinning a palette the operator never chose.
    /// </summary>
    public static DesktopThemeMode LoadMode(string rootDirectory, DesktopThemeMode fallback = DesktopThemeMode.System)
    {
        try
        {
            string text = File.ReadAllText(ThemePath(rootDirectory), Encoding.UTF8);
            return DesktopThemeModeExtensions.TryParsePreference(text, out DesktopThemeMode mode) ? mode : fallback;
        }
        catch (IOException)
        {
            return fallback;
        }
        catch (UnauthorizedAccessException)
        {
            return fallback;
        }
    }

    /// <summary>Persists the mode atomically, so a killed process cannot leave half a token.</summary>
    public static void SaveMode(string rootDirectory, DesktopThemeMode mode)
    {
        string directory = Path.Combine(rootDirectory, "runtime");
        Directory.CreateDirectory(directory);
        string path = ThemePath(rootDirectory);
        string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(temporary, mode.ToPreferenceText(), new UTF8Encoding(false));
            File.Move(temporary, path, overwrite: true);
        }
        finally
        {
            if (File.Exists(temporary))
                File.Delete(temporary);
        }
    }

    /// <summary>
    /// The palette for <paramref name="mode"/>, with <paramref name="systemTheme"/>
    /// standing in for <see cref="DesktopThemeMode.System"/>.
    /// </summary>
    /// <remarks>
    /// The parameter exists so a caller that already asked the OS does not pay for a
    /// second query, and so a test can pin the answer without touching the machine's
    /// theme. A caller that has no opinion passes
    /// <see cref="DesktopThemeModeExtensions.Resolve"/>'s own OS query instead.
    /// </remarks>
    public static ApplicationTheme Resolve(DesktopThemeMode mode, ApplicationTheme systemTheme)
        => mode == DesktopThemeMode.System ? systemTheme : mode.Resolve();

    /// <summary>
    /// Two-state read kept for callers that predate the mode: the file, interpreted
    /// against <paramref name="systemTheme"/> when it says <c>System</c> (or says
    /// nothing this build understands).
    /// </summary>
    public static ApplicationTheme Load(string rootDirectory, ApplicationTheme systemTheme)
        => Resolve(LoadMode(rootDirectory), systemTheme);

    /// <summary>Two-state write: only <see cref="ApplicationTheme.Light"/>/<see cref="ApplicationTheme.Dark"/> are storable.</summary>
    public static void Save(string rootDirectory, ApplicationTheme theme)
    {
        if (theme is not (ApplicationTheme.Light or ApplicationTheme.Dark))
            throw new ArgumentOutOfRangeException(nameof(theme));

        SaveMode(
            rootDirectory,
            theme == ApplicationTheme.Dark ? DesktopThemeMode.Dark : DesktopThemeMode.Light);
    }
}
