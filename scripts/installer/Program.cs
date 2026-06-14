using System;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Drawing;

internal static class Program
{
    private const string AppName = "GPT-Register-Tool";

    [STAThread]
    private static int Main(string[] args)
    {
        InstallOptions options = InstallOptions.Parse(args);
        if (options.Silent)
        {
            return RunSilentInstall(options);
        }

        ApplicationConfiguration.Initialize();
        using var form = new InstallerForm(options);
        Application.Run(form);
        return form.ExitCode;
    }

    private static int RunSilentInstall(InstallOptions options)
    {
        try
        {
            Install(options, progress: null);
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("Install failed: " + ex.Message);
            return 1;
        }
    }

    private static InstallResult Install(InstallOptions options, IProgress<string>? progress)
    {
        string installDir = Environment.ExpandEnvironmentVariables(options.InstallDir.Trim('"'));
        progress?.Report("Creating install directory...");
        Directory.CreateDirectory(installDir);

        progress?.Report("Extracting application files...");
        using Stream payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip")
            ?? throw new InvalidOperationException("Embedded payload.zip was not found.");
        ExtractZipSafely(payload, installDir, overwrite: true);

        progress?.Report("Preparing local configuration...");
        string exampleConfig = Path.Combine(installDir, "config.example.json");
        string config = Path.Combine(installDir, "config.json");
        if (!File.Exists(config) && File.Exists(exampleConfig))
        {
            File.Copy(exampleConfig, config);
        }

        string exePath = Path.Combine(installDir, "dist", "net10", "SmsWorkbench.exe");
        progress?.Report("Creating shortcuts...");
        WriteUninstaller(installDir);
        if (!options.NoDesktopShortcut)
        {
            CreateShortcut(
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "SmsWorkbench.lnk"),
                exePath,
                installDir);
        }
        if (!options.NoStartMenuShortcut)
        {
            string startMenuDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.StartMenu), "Programs", AppName);
            Directory.CreateDirectory(startMenuDir);
            CreateShortcut(Path.Combine(startMenuDir, "SmsWorkbench.lnk"), exePath, installDir);
            CreateShortcut(Path.Combine(startMenuDir, "Uninstall GPT-Register-Tool.lnk"), Path.Combine(installDir, "Uninstall.cmd"), installDir);
        }

        progress?.Report("Install complete.");
        if (options.Launch && File.Exists(exePath))
        {
            LaunchApp(exePath, installDir);
        }

        return new InstallResult(installDir, exePath);
    }

    private static void LaunchApp(string exePath, string installDir)
    {
        Process.Start(new ProcessStartInfo
        {
            FileName = exePath,
            WorkingDirectory = installDir,
            UseShellExecute = true,
        });
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

    private sealed class InstallerForm : Form
    {
        private readonly InstallOptions options;
        private readonly TextBox installPathBox = new();
        private readonly CheckBox desktopShortcutBox = new();
        private readonly CheckBox startMenuShortcutBox = new();
        private readonly CheckBox launchBox = new();
        private readonly Button installButton = new();
        private readonly Button cancelButton = new();
        private readonly ProgressBar progressBar = new();
        private readonly Label statusLabel = new();
        private readonly PictureBox iconBox = new();
        private bool installStarted;

        public int ExitCode { get; private set; } = 1;

        public InstallerForm(InstallOptions options)
        {
            this.options = options;
            InitializeComponent();
        }

        private void InitializeComponent()
        {
            Text = "GPT-Register-Tool Setup";
            StartPosition = FormStartPosition.CenterScreen;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            ClientSize = new Size(640, 360);
            Font = new Font("Segoe UI", 9F, FontStyle.Regular, GraphicsUnit.Point);
            Icon = LoadSetupIcon();

            iconBox.Location = new Point(28, 24);
            iconBox.Size = new Size(64, 64);
            iconBox.SizeMode = PictureBoxSizeMode.CenterImage;
            iconBox.Image = Icon?.ToBitmap();
            Controls.Add(iconBox);

            var titleLabel = new Label
            {
                Text = "Install GPT-Register-Tool",
                AutoSize = true,
                Font = new Font(Font.FontFamily, 15F, FontStyle.Bold),
                Location = new Point(110, 26),
            };
            Controls.Add(titleLabel);

            var subtitleLabel = new Label
            {
                Text = "Choose an install location, then install the desktop workbench and project runtime files.",
                AutoSize = true,
                Location = new Point(112, 60),
            };
            Controls.Add(subtitleLabel);

            var pathLabel = new Label
            {
                Text = "Install path:",
                AutoSize = true,
                Location = new Point(32, 116),
            };
            Controls.Add(pathLabel);

            installPathBox.Location = new Point(32, 140);
            installPathBox.Size = new Size(486, 27);
            installPathBox.Text = options.InstallDir;
            Controls.Add(installPathBox);

            var browseButton = new Button
            {
                Text = "Browse...",
                Location = new Point(530, 138),
                Size = new Size(82, 31),
            };
            browseButton.Click += (_, _) => BrowseInstallPath();
            Controls.Add(browseButton);

            desktopShortcutBox.Text = "Create desktop shortcut";
            desktopShortcutBox.AutoSize = true;
            desktopShortcutBox.Checked = !options.NoDesktopShortcut;
            desktopShortcutBox.Location = new Point(32, 190);
            Controls.Add(desktopShortcutBox);

            startMenuShortcutBox.Text = "Create Start Menu shortcuts";
            startMenuShortcutBox.AutoSize = true;
            startMenuShortcutBox.Checked = !options.NoStartMenuShortcut;
            startMenuShortcutBox.Location = new Point(32, 218);
            Controls.Add(startMenuShortcutBox);

            launchBox.Text = "Launch SmsWorkbench after install";
            launchBox.AutoSize = true;
            launchBox.Checked = options.Launch;
            launchBox.Location = new Point(32, 246);
            Controls.Add(launchBox);

            progressBar.Location = new Point(32, 284);
            progressBar.Size = new Size(580, 18);
            progressBar.Style = ProgressBarStyle.Continuous;
            Controls.Add(progressBar);

            statusLabel.Text = "Ready to install.";
            statusLabel.AutoSize = true;
            statusLabel.Location = new Point(32, 310);
            Controls.Add(statusLabel);

            installButton.Text = "Install";
            installButton.Location = new Point(430, 322);
            installButton.Size = new Size(88, 30);
            installButton.Click += async (_, _) => await InstallAsync();
            Controls.Add(installButton);

            cancelButton.Text = "Cancel";
            cancelButton.Location = new Point(524, 322);
            cancelButton.Size = new Size(88, 30);
            cancelButton.Click += (_, _) => Close();
            Controls.Add(cancelButton);

            AcceptButton = installButton;
            CancelButton = cancelButton;
        }

        private static Icon? LoadSetupIcon()
        {
            try
            {
                string exePath = Application.ExecutablePath;
                return Icon.ExtractAssociatedIcon(exePath);
            }
            catch
            {
                return null;
            }
        }

        private void BrowseInstallPath()
        {
            using var dialog = new FolderBrowserDialog
            {
                Description = "Choose where GPT-Register-Tool should be installed.",
                SelectedPath = installPathBox.Text,
                UseDescriptionForTitle = true,
            };
            if (dialog.ShowDialog(this) == DialogResult.OK)
            {
                installPathBox.Text = dialog.SelectedPath;
            }
        }

        private async Task InstallAsync()
        {
            if (installStarted)
            {
                return;
            }

            string installDir = installPathBox.Text.Trim();
            if (string.IsNullOrWhiteSpace(installDir))
            {
                MessageBox.Show(this, "Please choose an install path.", Text, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }

            installStarted = true;
            installButton.Enabled = false;
            cancelButton.Enabled = false;
            progressBar.Style = ProgressBarStyle.Marquee;
            statusLabel.Text = "Installing...";

            var runOptions = options with
            {
                InstallDir = installDir,
                NoDesktopShortcut = !desktopShortcutBox.Checked,
                NoStartMenuShortcut = !startMenuShortcutBox.Checked,
                Launch = launchBox.Checked,
            };
            var progress = new Progress<string>(message => statusLabel.Text = message);

            try
            {
                InstallResult result = await Task.Run(() => Install(runOptions, progress));
                progressBar.Style = ProgressBarStyle.Continuous;
                progressBar.Value = 100;
                statusLabel.Text = "Install complete.";
                ExitCode = 0;
                MessageBox.Show(this, $"GPT-Register-Tool was installed successfully.\n\n{result.InstallDir}", Text, MessageBoxButtons.OK, MessageBoxIcon.Information);
                Close();
            }
            catch (Exception ex)
            {
                progressBar.Style = ProgressBarStyle.Continuous;
                progressBar.Value = 0;
                statusLabel.Text = "Install failed.";
                installStarted = false;
                installButton.Enabled = true;
                cancelButton.Enabled = true;
                MessageBox.Show(this, "Install failed:\n\n" + ex.Message, Text, MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }
    }

    private sealed record InstallResult(string InstallDir, string ExePath);

    private sealed record InstallOptions(
        bool Silent,
        bool NoDesktopShortcut,
        bool NoStartMenuShortcut,
        bool Launch,
        string InstallDir)
    {
        public static InstallOptions Parse(string[] args)
        {
            return new InstallOptions(
                Silent: HasArg(args, "/S") || HasArg(args, "/Silent") || HasArg(args, "--silent"),
                NoDesktopShortcut: HasArg(args, "/NoDesktopShortcut") || HasArg(args, "--no-desktop-shortcut"),
                NoStartMenuShortcut: HasArg(args, "/NoStartMenuShortcut") || HasArg(args, "--no-start-menu-shortcut"),
                Launch: HasArg(args, "/Launch") || HasArg(args, "--launch"),
                InstallDir: GetValueArg(args, "/DIR=")
                    ?? GetValueArg(args, "--dir=")
                    ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), AppName));
        }
    }
}
