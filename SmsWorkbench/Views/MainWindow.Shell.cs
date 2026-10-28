// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
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
            Window dialog = DialogFactory.Create(
                this,
                SmsWorkbench.Localization.Loc.T("外壳设置"),
                width: 480,
                height: 236,
                minWidth: 420,
                minHeight: 220);

            Grid root = DialogFactory.CreateRootGrid();
            var body = new StackPanel();
            CheckBox trayBox = DialogFactory.CreateCheckBox(
                dialog,
                SmsWorkbench.Localization.Loc.T("关闭窗口时隐藏到托盘"),
                shellPreferences.Tray);
            CheckBox hotkeyBox = DialogFactory.CreateCheckBox(
                dialog,
                SmsWorkbench.Localization.Loc.F("启用全局热键 {0}", GlobalHotkeyService.DefaultLabel),
                shellPreferences.Hotkey);
            body.Children.Add(DialogFactory.CreateFormLabel(dialog, SmsWorkbench.Localization.Loc.T("窗口关闭与唤起")));
            body.Children.Add(trayBox);
            body.Children.Add(hotkeyBox);
            Grid.SetRow(body, 0);
            root.Children.Add(body);

            StackPanel actions = DialogFactory.CreateActionRow();
            Button cancel = DialogFactory.CreateCancelButton();
            cancel.Click += (_, _) => dialog.Close();
            Button save = DialogFactory.CreatePrimaryButton(dialog, SmsWorkbench.Localization.Loc.T("保存"));
            save.Click += (_, _) =>
            {
                ApplyShellPreferences(new DesktopShellPreferences(
                    Tray: trayBox.IsChecked == true,
                    Hotkey: hotkeyBox.IsChecked == true));
                dialog.Close();
            };
            actions.Children.Add(cancel);
            actions.Children.Add(save);
            Grid.SetRow(actions, 1);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
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
