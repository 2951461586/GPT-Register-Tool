namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Backend process, task list, deletion and cancellation actions
        private void RerunFailed_Click(object sender, RoutedEventArgs e)
        {
            var failedRows = allRows.Where(r =>
                (r.Status.Contains("失败") || r.Status.Contains("待处理") || r.Status.Contains('缺'))
                && IsMailboxPoolLikeRow(r)
                && !string.IsNullOrWhiteSpace(r.RawLine)).ToList();

            if (failedRows.Count == 0)
            {
                MessageBox.Show("没有找到需要重注册的失败账号。", "提示", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            if (MessageBox.Show($"找到 {failedRows.Count} 条失败/待处理账号，确定重新注册？\n\n流程：注册→获取 access token→存 session 入库",
                "确认重注册", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return;

            if (!TryCreateMailboxFile(failedRows, out string mailboxArg, out string tempFile, out int mailboxCount))
            {
                MessageBox.Show("失败记录缺少可用邮箱凭据，无法重新注册。", "格式不匹配", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            var args = new List<string> { mailboxArg, tempFile, "--count", mailboxCount.ToString(CultureInfo.InvariantCulture), "--workers", "4" };
            AddNoPhoneRegistrationArgs(args);
            AddRegistrationProxy(args);
            RunBackend("重新注册失败账号 (" + mailboxCount + ")", args);
        }

        private void RebuildSqlite_Click(object sender, RoutedEventArgs e)
        {
            var args = new List<string> { "--rebuild-sqlite" };
            RunBackend("重建SQLite索引", args);
        }

        private void AccountGrid_SelectionChanged(object sender, SelectionChangedEventArgs e)
        {
            foreach (object item in e.AddedItems)
            {
                if (item is PoolRow row) row.IsChecked = true;
            }
        }

        private void AccountDetail_Click(object sender, RoutedEventArgs e)
        {
            if (sender is FrameworkElement element && element.DataContext is PoolRow row)
            {
                ShowAccountDetail(row);
            }
        }

        private async void RunBackend(string taskName, List<string> args)
        {
            if (backendTasks.IsRunning)
            {
                MessageBox.Show("已有批次正在运行，请先取消或等待完成。", "运行中", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            string safeArgs = FormatBackendArgsForDisplay(args);
            var task = new TaskRow { Name = "批次 " + taskSeq++, Task = taskName, Status = "运行中", Info = safeArgs };
            Tasks.Add(task);
            ScrollTaskGridToBottom();
            DateTime started = DateTime.Now;

            var backendOutput = new StringBuilder();
            object backendOutputLock = new object();
            void CaptureBackendLine(string line)
            {
                lock (backendOutputLock)
                {
                    backendOutput.AppendLine(line);
                }
            }

            var progress = new Progress<BackendOutputLine>(line =>
            {
                CaptureBackendLine(line.Text);
                UiLog(line.Text);
            });
            try
            {
                Log("启动：python " + safeArgs);
                StatusText = taskName + " 运行中";
                BackendCommandResult result = await backendTasks.RunAsync(
                    BackendCommand.Create(taskName, args, 12 * 60 * 60 * 1000),
                    progress);

                task.Status = result.ExitCode == 0 ? "完成" : "失败";
                task.Cost = ((int)(DateTime.Now - started).TotalSeconds).ToString(CultureInfo.InvariantCulture);
                task.DoneAt = SafeTime(DateTime.Now);
                StatusText = taskName + " 已结束";
                RefreshPools();
                ScrollTaskGridToBottom();
                if (taskName.StartsWith("账号测活", StringComparison.OrdinalIgnoreCase))
                {
                    string output;
                    lock (backendOutputLock)
                    {
                        output = backendOutput.ToString();
                    }
                    ShowAccountScanResultDialog(output);
                }
            }
            catch (OperationCanceledException)
            {
                task.Status = "已取消";
                task.DoneAt = SafeTime(DateTime.Now);
                StatusText = taskName + " 已取消";
            }
            catch (BackendTaskAlreadyRunningException)
            {
                task.Status = "未启动";
                task.DoneAt = SafeTime(DateTime.Now);
                StatusText = taskName + " 未启动";
                MessageBox.Show("已有批次正在运行，请先取消或等待完成。", "运行中", MessageBoxButton.OK, MessageBoxImage.Information);
            }
            catch (Exception ex)
            {
                task.Status = "启动失败";
                Log("启动失败：" + ex.Message);
            }
        }

        private string RunBackendWithResult(string taskName, List<string> args, int timeoutMs = 120000)
        {
            Log("启动：python " + FormatBackendArgsForDisplay(args));
            return backendTasks.RunForResultAsync(
                BackendCommand.Create(taskName, args, timeoutMs)).GetAwaiter().GetResult();
        }

        private static string FormatBackendArgsForDisplay(List<string> args)
        {
            return SensitiveDataSanitizer.RedactArguments(args);
        }

        private void TaskGrid_Loaded(object sender, RoutedEventArgs e) => ScrollTaskGridToBottom();

        private void ScrollTaskGridToBottom()
        {
            if (TaskGrid == null || Tasks.Count == 0) return;
            Dispatcher.BeginInvoke(new Action(() =>
            {
                object last = Tasks[Tasks.Count - 1];
                TaskGrid.SelectedItem = last;
                TaskGrid.ScrollIntoView(last);
            }), DispatcherPriority.Background);
        }

        private async void DeleteSelected_Click(object sender, RoutedEventArgs e)
        {
            var selected = SelectedEmailRowsOrNotify("删除");
            if (selected.Count == 0) return;
            if (!await ShowDeleteConfirmDialog(selected.Count)) return;
            int failed = 0;
            foreach (PoolRow row in selected)
            {
                if (!await DeleteRowAsync(row)) failed++;
            }
            RefreshPools();
            if (failed > 0)
            {
                await DialogFactory.ShowInfoAsync(
                    this,
                    "删除未完成",
                    failed + " 条记录未能完整删除。请查看运行日志。");
            }
        }

        private async Task<bool> ShowDeleteConfirmDialog(int count)
        {
            return await DialogFactory.ShowConfirmAsync(
                this,
                "删除选中的 " + count + " 条记录？",
                "将同步清理本地邮箱池、SQLite 索引和匹配的 session 文件。此操作不可撤销。",
                "删除",
                isDanger: true);
        }

        private async Task<bool> DeleteRowAsync(PoolRow row)
        {
            try
            {
                string emailKey = NormalizeEmailKey(row.Identifier);
                if (emailKey.Length == 0) return false;
                var args = new List<string> { "--delete-account", "--email", emailKey, "--desktop-ipc" };
                BackendCommandResult backend = await backendTasks.RunAsync(
                    BackendCommand.Create("删除账号", args, 120000));
                if (backend.ExitCode != 0 || !backend.Payload.HasValue)
                {
                    Log("删除失败：" + SensitiveDataSanitizer.Redact(emailKey));
                    return false;
                }
                Log("删除账号完成：" + SensitiveDataSanitizer.Redact(emailKey));
                return true;
#if LEGACY_DELETE_CODE
#pragma warning disable CS0162
                string legacyEmailKey = NormalizeEmailKey(row.Identifier);
                int removedPoolLines = DeleteMailboxLines(row, emailKey);
                int removedSqliteRows = DeleteSqliteAccountRows(row, emailKey);
                int removedSessionFiles = DeleteSessionJsonFiles(row, emailKey);

                if (row.SourcePath.EndsWith(".json", StringComparison.OrdinalIgnoreCase)
                    && File.Exists(row.SourcePath)
                    && IsUnderDirectory(row.SourcePath, GetSessionsDir()))
                {
                    File.Delete(row.SourcePath);
                    removedSessionFiles++;
                }

                Log("删除账号：" + row.Identifier
                    + "，邮箱池 " + removedPoolLines
                    + " 条，SQLite " + removedSqliteRows
                    + " 条，session " + removedSessionFiles + " 个");
                return true;
#endif
            }
#pragma warning restore CS0162
            catch (Exception ex)
            {
                Log("删除失败：" + SensitiveDataSanitizer.Redact(row.Identifier) + " " + SensitiveDataSanitizer.Redact(ex.Message));
                return false;
            }
        }

#if LEGACY_DELETE_CODE
        private bool DeletionEmailMatch(string candidate, string emailKey)
        {
            if (emailKey.Length == 0) return false;
            string normalizedCandidate = NormalizeEmailKey(candidate);
            return normalizedCandidate.Length > 0 && normalizedCandidate == emailKey;
        }

        private int DeleteMailboxLines(PoolRow row, string emailKey)
        {
            int removed = 0;
            var paths = GetKnownMailboxPoolFiles().ToList();
            if (!string.IsNullOrWhiteSpace(row.SourcePath)
                && row.SourcePath.EndsWith(".txt", StringComparison.OrdinalIgnoreCase)
                && File.Exists(row.SourcePath))
            {
                paths.Insert(0, row.SourcePath);
            }
            var exactLines = new[] { row.RawLine, row.MailboxLine };
            foreach (string path in paths.Where(p => !string.IsNullOrWhiteSpace(p)).Distinct(StringComparer.OrdinalIgnoreCase))
            {
                removed += MailboxPoolFileStore.DeleteMatchingLines(path, emailKey, exactLines);
            }
            return removed;
        }

        private int DeleteSqliteAccountRows(PoolRow row, string emailKey)
        {
            string dbPath = row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase)
                ? row.SourcePath
                : GetDatabasePath();
            if (!File.Exists(dbPath)) return 0;

            var rows = SqliteNative.Query(dbPath, "SELECT id,email,json_path FROM accounts");
            var deleteIds = new List<string>();
            string explicitId = row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase) ? OnlyDigits(row.RawLine) : "";
            foreach (Dictionary<string, string> data in rows)
            {
                string id = data.TryGetValue("id", out string rawId) ? rawId : "";
                string email = data.TryGetValue("email", out string rawEmail) ? rawEmail : "";
                bool matches = explicitId.Length > 0 && id == explicitId;
                matches = matches || DeletionEmailMatch(email, emailKey);
                if (!matches) continue;
                deleteIds.Add(id);

                string jsonPath = data.TryGetValue("json_path", out string rawJsonPath) ? rawJsonPath : "";
                if (File.Exists(jsonPath) && IsUnderDirectory(jsonPath, GetSessionsDir()))
                {
                    TryDeleteFile(jsonPath);
                }
            }

            foreach (string id in deleteIds.Distinct())
            {
                SqliteNative.Execute(dbPath, "DELETE FROM accounts WHERE id=" + OnlyDigits(id));
            }
            return deleteIds.Distinct().Count();
        }

        private int DeleteSessionJsonFiles(PoolRow row, string emailKey)
        {
            int removed = 0;
            var dirs = new List<string> { GetSessionsDir(), rootDir };
            foreach (string dir in dirs.Where(Directory.Exists).Distinct(StringComparer.OrdinalIgnoreCase))
            {
                foreach (string path in Directory.GetFiles(dir, "session_*.json", SearchOption.TopDirectoryOnly))
                {
                    if (!SessionJsonMatchesEmail(path, emailKey)) continue;
                    if (TryDeleteFile(path)) removed++;
                }
            }
            string notes = (row.Notes ?? "").Trim();
            if (File.Exists(notes) && notes.EndsWith(".json", StringComparison.OrdinalIgnoreCase)
                && IsUnderDirectory(notes, GetSessionsDir()) && TryDeleteFile(notes))
            {
                removed++;
            }
            return removed;
        }

        private bool SessionJsonMatchesEmail(string path, string emailKey)
        {
            if (emailKey.Length == 0) return false;
            try
            {
                Dictionary<string, object> data = ReadJsonObject(path);
                return DeletionEmailMatch(GetString(data, "email"), emailKey);
            }
            catch
            {
                return false;
            }
        }

        private bool TryDeleteFile(string path)
        {
            try
            {
                if (!File.Exists(path)) return false;
                File.Delete(path);
                return true;
            }
            catch (Exception ex)
            {
                Log("删除文件失败：" + path + " " + ex.Message);
                return false;
            }
        }
#endif

        private bool TryDeleteFile(string path)
        {
            try
            {
                if (!File.Exists(path)) return false;
                File.Delete(path);
                return true;
            }
            catch (Exception ex)
            {
                Log("删除文件失败：" + SensitiveDataSanitizer.Redact(path) + " " + SensitiveDataSanitizer.Redact(ex.Message));
                return false;
            }
        }

        private void CancelBatch_Click(object sender, RoutedEventArgs e)
        {
            if (!backendTasks.IsRunning)
            {
                Log("当前没有运行中的批次。");
                return;
            }
            try
            {
                if (backendTasks.Cancel())
                    Log("已取消当前批次。");
            }
            catch (Exception ex)
            {
                Log("取消失败：" + ex.Message);
            }
        }

        private void Refresh_Click(object sender, RoutedEventArgs e) => RefreshPools();

        private void Settings_Click(object sender, RoutedEventArgs e) => ShowConfigDialog();
    }
}
