// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Workspace backup / restore entries from the sidebar.
    /// </summary>
    /// <remarks>
    /// The file dialogs live here, the file rules live in
    /// <see cref="IWorkspaceBackupService"/>: what may be archived, what a restore may
    /// overwrite, and where the pre-restore copies go are contract decisions and are
    /// tested without a window.
    /// </remarks>
    public partial class MainWindow
    {
        private void BackupWorkspace()
        {
            var dialog = new Microsoft.Win32.SaveFileDialog
            {
                Title = SmsWorkbench.Localization.Loc.T("备份工作区"),
                Filter = "Zip (*.zip)|*.zip",
                DefaultExt = ".zip",
                AddExtension = true,
                FileName = "workspace-" + DateTime.Now.ToString("yyyyMMdd-HHmm", CultureInfo.InvariantCulture) + ".zip",
            };
            if (dialog.ShowDialog(this) != true) return;

            try
            {
                WorkspaceBackupResult result = workspaceBackup.Export(dialog.FileName);
                Log($"工作区已备份: {result.Entries} 个文件 / {result.Bytes / 1024} KB -> {result.ArchivePath}");
                NotifySuccess(SmsWorkbench.Localization.Loc.F("已备份 {0} 个文件", result.Entries));
            }
            catch (Exception ex)
            {
                Log($"备份失败: {ex.Message}");
                ShowThemedInfoDialog(SmsWorkbench.Localization.Loc.T("备份工作区"), ex.Message);
            }
        }

        private void RestoreWorkspace()
        {
            var dialog = new Microsoft.Win32.OpenFileDialog
            {
                Title = SmsWorkbench.Localization.Loc.T("恢复工作区"),
                Filter = "Zip (*.zip)|*.zip",
                CheckFileExists = true,
            };
            if (dialog.ShowDialog(this) != true) return;

            try
            {
                WorkspaceRestoreResult result = workspaceBackup.Restore(dialog.FileName);
                Log($"工作区已恢复: {result.Restored} 个文件，跳过 {result.Skipped} 个；旧副本: {result.SafetyDirectory}");
                NotifySuccess(SmsWorkbench.Localization.Loc.F("已恢复 {0} 个文件", result.Restored));
                // Config and the account DB both changed under the running shell.
                RefreshPools();
            }
            catch (Exception ex)
            {
                Log($"恢复失败: {ex.Message}");
                ShowThemedInfoDialog(SmsWorkbench.Localization.Loc.T("恢复工作区"), ex.Message);
            }
        }
    }
}
