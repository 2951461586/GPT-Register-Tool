using System;
using System.IO;
using System.Text;
using Wpf.Ui.Appearance;

namespace SmsWorkbench;

internal static class DesktopThemePreference
{
    private static string ThemePath(string rootDirectory)
        => Path.Combine(rootDirectory, "runtime", "desktop_theme.txt");

    public static ApplicationTheme Load(string rootDirectory, ApplicationTheme systemTheme)
    {
        try
        {
            return File.ReadAllText(ThemePath(rootDirectory), Encoding.UTF8).Trim() switch
            {
                "Dark" => ApplicationTheme.Dark,
                "Light" => ApplicationTheme.Light,
                _ => systemTheme,
            };
        }
        catch (IOException)
        {
            return systemTheme;
        }
        catch (UnauthorizedAccessException)
        {
            return systemTheme;
        }
    }

    public static void Save(string rootDirectory, ApplicationTheme theme)
    {
        if (theme is not (ApplicationTheme.Light or ApplicationTheme.Dark))
            throw new ArgumentOutOfRangeException(nameof(theme));

        string directory = Path.Combine(rootDirectory, "runtime");
        Directory.CreateDirectory(directory);
        string path = ThemePath(rootDirectory);
        string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(temporary, theme.ToString(), new UTF8Encoding(false));
            File.Move(temporary, path, overwrite: true);
        }
        finally
        {
            if (File.Exists(temporary))
                File.Delete(temporary);
        }
    }
}
