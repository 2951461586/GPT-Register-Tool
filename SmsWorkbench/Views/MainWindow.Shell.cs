namespace SmsWorkbench.Views
{
    /// <summary>
    /// The two shell conveniences that change what "close" and "focus" do.
    /// </summary>
    /// <remarks>
    /// The switches exist because both are intrusive when unwanted: a tray icon adds a
    /// second entry point to quit, and a global hotkey takes a combination away from
    /// every other application on the machine. Defaults stay on (a hidden window with no
    /// tray and no hotkey is unreachable), and turning them off is one dialog away.
    /// </remarks>
    public partial class MainWindow
    {
        private DesktopShellPreferences shellPreferences = DesktopShellPreferences.Default;

        /// <summary>Sidebar entry point: show the current switches and apply the new ones.</summary>
        private void ShowShellSettings()
        {
            var dialog = new ShellSettingsDialog(this, shellPreferences, GlobalHotkeyService.DefaultLabel);
            if (dialog.ShowDialog() == true && dialog.SelectedPreferences is { } next)
            {
                ApplyShellPreferences(next);
            }
        }

        /// <summary>
        /// Applies the switches to the live shell and persists them.
        /// </summary>
        /// <remarks>
        /// Applied before saving: a switch that cannot take effect (the hotkey is owned by
        /// another process) must report that, not look successful because the file was
        /// written.
        /// </remarks>
        private void ApplyShellPreferences(DesktopShellPreferences preferences)
        {
            shellPreferences = preferences;

            if (preferences.Tray)
            {
                trayIcon.Initialize();
            }
            else
            {
                // Drops the icon and its message window; closing now goes straight to exit.
                trayIcon.Dispose();
            }

            if (preferences.Hotkey)
            {
                if (!globalHotkey.TryRegister(this, GlobalHotkeyService.DefaultModifiers, GlobalHotkeyService.DefaultKey))
                {
                    Log($"热键 {GlobalHotkeyService.DefaultLabel} 注册失败：可能已被其它程序占用。");
                    NotifyWarning(SmsWorkbench.Localization.Loc.F(
                        "热键 {0} 注册失败：可能已被其它程序占用",
                        GlobalHotkeyService.DefaultLabel));
                }
            }
            else
            {
                globalHotkey.Dispose();
            }

            try
            {
                DesktopShellPreference.Save(rootDir, preferences);
                Log($"外壳偏好已保存: 托盘={preferences.Tray} 热键={preferences.Hotkey}");
            }
            catch (Exception ex)
            {
                Log($"保存外壳偏好失败: {ex.Message}");
            }
        }
    }
}
