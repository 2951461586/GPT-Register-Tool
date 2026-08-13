namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Settings dialog entry point.  Config reads go through ISettingsService
        // (GetString/GetStringList) and writes through ISettingsService.UpdateConfig,
        // which preserves unknown fields and replaces the file atomically.
        private void ShowConfigDialog()
        {
            if (settingsDialogs.ShowDialog(this))
                Log("配置已保存。");
        }
    }
}
