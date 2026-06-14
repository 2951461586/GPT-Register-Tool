using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;

internal static class Program
{
    private const string AppName = "GPT-Register-Tool";

    private static int Main(string[] args)
    {
        try
        {
            bool silent = HasArg(args, "/S") || HasArg(args, "/Silent") || HasArg(args, "--silent");
            bool noDesktopShortcut = HasArg(args, "/NoDesktopShortcut") || HasArg(args, "--no-desktop-shortcut");
            bool noStartMenuShortcut = HasArg(args, "/NoStartMenuShortcut") || HasArg(args, "--no-start-menu-shortcut");
            bool launch = HasArg(args, "/Launch") || HasArg(args, "--launch");
            string installDir = GetValueArg(args, "/DIR=")
                ?? GetValueArg(args, "--dir=")
                ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), AppName);

            installDir = Environment.ExpandEnvironmentVariables(installDir.Trim('"'));
            Directory.CreateDirectory(installDir);

            using Stream payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip")
                ?? throw new InvalidOperationException("Embedded payload.zip was not found.");
            ExtractZipSafely(payload, installDir, overwrite: true);

            string exampleConfig = Path.Combine(installDir, "config.example.json");
            string config = Path.Combine(installDir, "config.json");
            if (!File.Exists(config) && File.Exists(exampleConfig))
            {
                File.Copy(exampleConfig, config);
            }

            string exePath = Path.Combine(installDir, "dist", "net10", "SmsWorkbench.exe");
            WriteUninstaller(installDir);
            if (!noDesktopShortcut)
            {
                CreateShortcut(
                    Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "SmsWorkbench.lnk"),
                    exePath,
                    installDir);
            }
            if (!noStartMenuShortcut)
            {
                string startMenuDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.StartMenu), "Programs", AppName);
                Directory.CreateDirectory(startMenuDir);
                CreateShortcut(Path.Combine(startMenuDir, "SmsWorkbench.lnk"), exePath, installDir);
                CreateShortcut(Path.Combine(startMenuDir, "Uninstall GPT-Register-Tool.lnk"), Path.Combine(installDir, "Uninstall.cmd"), installDir);
            }

            if (!silent)
            {
                Console.WriteLine($"Installed {AppName} to: {installDir}");
                Console.WriteLine($"Desktop app: {exePath}");
                Console.WriteLine("Edit config.json before running production flows.");
            }

            if (launch && File.Exists(exePath))
            {
                Process.Start(new ProcessStartInfo
                {
                    FileName = exePath,
                    WorkingDirectory = installDir,
                    UseShellExecute = true,
                });
            }
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("Install failed: " + ex.Message);
            return 1;
        }
    }

    private static bool HasArg(string[] args, string value)
    {
        foreach (string arg in args)
        {
            if (string.Equals(arg, value, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }
        return false;
    }

    private static string? GetValueArg(string[] args, string prefix)
    {
        foreach (string arg in args)
        {
            if (arg.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            {
                return arg[prefix.Length..];
            }
        }
        return null;
    }

    private static void ExtractZipSafely(Stream payload, string destination, bool overwrite)
    {
        string destinationRoot = Path.GetFullPath(destination).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar;
        using var archive = new ZipArchive(payload, ZipArchiveMode.Read);
        foreach (ZipArchiveEntry entry in archive.Entries)
        {
            string targetPath = Path.GetFullPath(Path.Combine(destinationRoot, entry.FullName));
            if (!targetPath.StartsWith(destinationRoot, StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException($"Refusing to extract unsafe entry: {entry.FullName}");
            }

            if (entry.FullName.EndsWith("/", StringComparison.Ordinal) || entry.FullName.EndsWith("\\", StringComparison.Ordinal))
            {
                Directory.CreateDirectory(targetPath);
                continue;
            }

            string? parent = Path.GetDirectoryName(targetPath);
            if (!string.IsNullOrEmpty(parent))
            {
                Directory.CreateDirectory(parent);
            }
            entry.ExtractToFile(targetPath, overwrite);
        }
    }

    private static void WriteUninstaller(string installDir)
    {
        string uninstallPath = Path.Combine(installDir, "Uninstall.cmd");
        string desktopShortcut = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "SmsWorkbench.lnk");
        string startMenuDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.StartMenu), "Programs", AppName);
        string escapedInstallDir = installDir.Replace("'", "''");
        File.WriteAllLines(uninstallPath, new[]
        {
            "@echo off",
            "setlocal",
            $@"rmdir /s /q ""{startMenuDir}"" 2>nul",
            $@"del /q ""{desktopShortcut}"" 2>nul",
            "cd /d \"%TEMP%\"",
            "start \"\" powershell -NoProfile -ExecutionPolicy Bypass -Command \"Start-Sleep -Seconds 2; Remove-Item -LiteralPath '" + escapedInstallDir + "' -Recurse -Force\""
        });
    }

    private static void CreateShortcut(string shortcutPath, string targetPath, string workingDirectory)
    {
        if (!RuntimeInformation.IsOSPlatform(OSPlatform.Windows))
        {
            return;
        }
        try
        {
            Type? shellType = Type.GetTypeFromProgID("WScript.Shell");
            if (shellType is null)
            {
                return;
            }
            dynamic shell = Activator.CreateInstance(shellType)!;
            dynamic shortcut = shell.CreateShortcut(shortcutPath);
            shortcut.TargetPath = targetPath;
            shortcut.WorkingDirectory = workingDirectory;
            shortcut.IconLocation = targetPath;
            shortcut.Save();
        }
        catch
        {
            // Shortcut creation is best-effort; extraction is the critical install step.
        }
    }
}
