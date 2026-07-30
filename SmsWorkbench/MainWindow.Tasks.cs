namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Backend process, task list, deletion and cancellation actions
        private void RerunFailed_Click(object sender, RoutedEventArgs e)
        {
            var failedRows = allRows.Where(r =>
                (r.Status.Contains("失败") || r.Status.Contains("待处理") || r.Status.Contains("缺"))
                && IsMailboxPoolLikeRow(r)
                && !string.IsNullOrWhiteSpace(r.RawLine)).ToList();

            if (failedRows.Count == 0)
            {
                MessageBox.Show("没有找到需要重注册的失败账号。", "提示", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            if (MessageBox.Show($"找到 {failedRows.Count} 条失败/待处理账号，确定重新注册？\n\n流程：注册→获取access token→生成支付链接→存session入库",
                "确认重注册", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return;

            string tempFile = Path.Combine(Path.GetTempPath(), "rerun_failed_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            var lines = new List<string>();
            foreach (PoolRow row in failedRows)
            {
                string line = row.RawLine.Trim();
                if (line.Length > 0) lines.Add(line);
            }
            File.WriteAllLines(tempFile, lines, new UTF8Encoding(false));

            var args = new List<string> { "--chatai-mailbox-file", tempFile, "--count", lines.Count.ToString(), "--workers", "4" };
            AddRegistrationProxy(args);
            AddPaypalOption(args);
            RunBackend("重新注册失败账号 (" + lines.Count + ")", args);
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
            if (runningProcess != null && !runningProcess.HasExited)
            {
                MessageBox.Show("已有批次正在运行，请先取消或等待完成。", "运行中", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            string script = Path.Combine(rootDir, "chatgpt_phone_reg.py");
            if (!File.Exists(script))
            {
                MessageBox.Show("找不到后端脚本：" + script, "错误", MessageBoxButton.OK, MessageBoxImage.Error);
                return;
            }

            string safeArgs = FormatBackendArgsForDisplay(args);
            var task = new TaskRow { Name = "批次 " + taskSeq++, Task = taskName, Status = "运行中", Info = safeArgs };
            Tasks.Add(task);
            ScrollTaskGridToBottom();
            DateTime started = DateTime.Now;

            var psi = new ProcessStartInfo
            {
                FileName = "python",
                Arguments = Quote(script) + " " + JoinArgs(args),
                WorkingDirectory = rootDir,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };

            var backendOutput = new StringBuilder();
            object backendOutputLock = new object();
            void CaptureBackendLine(string line)
            {
                lock (backendOutputLock)
                {
                    backendOutput.AppendLine(line);
                }
            }

            var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
            runningProcess = process;
            process.OutputDataReceived += (_, ev) =>
            {
                if (ev.Data == null) return;
                CaptureBackendLine(ev.Data);
                UiLog(ev.Data);
            };
            process.ErrorDataReceived += (_, ev) =>
            {
                if (ev.Data == null) return;
                CaptureBackendLine(ev.Data);
                UiLog(ev.Data);
            };

            try
            {
                Log("启动：" + psi.FileName + " " + Quote(script) + " " + safeArgs);
                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                StatusText = taskName + " 运行中";

                // async/await — no Dispatcher.BeginInvoke needed, returns to UI thread automatically
                await process.WaitForExitAsync();

                task.Status = process.ExitCode == 0 ? "完成" : "失败";
                task.Cost = ((int)(DateTime.Now - started).TotalSeconds).ToString();
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
            catch (Exception ex)
            {
                task.Status = "启动失败";
                Log("启动失败：" + ex.Message);
            }
        }

        private string RunBackendWithResult(string taskName, List<string> args, int timeoutMs = 120000)
        {
            Log("启动：python " + FormatBackendArgsForDisplay(args));
            var (stdout, stderr, exitCode) = CliLauncher.RunSync(rootDir, args, Quote, JoinArgs, timeoutMs);

            // 从 stdout 中提取最后一个完整 JSON 块；不能只取最后一个“{”，结果里可能有嵌套对象。
            if (!string.IsNullOrEmpty(stdout))
            {
                for (int start = stdout.LastIndexOf('{'); start >= 0; start = stdout.LastIndexOf('{', start - 1))
                {
                    string candidate = stdout.Substring(start).Trim();
                    try
                    {
                        using JsonDocument _ = JsonDocument.Parse(candidate);
                        return candidate;
                    }
                    catch (JsonException)
                    {
                    }
                }
                return stdout;
            }

            if (!string.IsNullOrEmpty(stderr))
                throw new Exception(stderr);

            return stdout;
        }

        private string FormatBackendArgsForDisplay(List<string> args)
        {
            var sensitiveOptions = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
            {
                "--at", "--access-token", "--refresh-token", "--api-key", "--api-token",
                "--admin-token", "--client-secret", "--password", "--service-token",
                "--proxy", "--proxy-pool", "--checkout-proxy", "--provider-proxy", "--approve-proxy",
                "--promotion-proxy", "--stripe-init-proxy", "--payment-method-proxy",
                "--confirm-proxy", "--redirect-proxy", "--blik-code", "--mailbox-line",
            };
            var output = new List<string>();
            for (int index = 0; index < (args?.Count ?? 0); index++)
            {
                string value = args[index] ?? "";
                int equals = value.IndexOf('=');
                string option = equals > 0 ? value.Substring(0, equals) : value;
                if (sensitiveOptions.Contains(option))
                {
                    output.Add(equals > 0 ? option + "=***" : option);
                    if (equals < 0 && index + 1 < args.Count)
                    {
                        output.Add("***");
                        index++;
                    }
                    continue;
                }
                output.Add(value);
            }
            return string.Join(" ", output);
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
            int failed = selected.Count(row => !DeleteRow(row));
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

        private bool DeleteRow(PoolRow row)
        {
            try
            {
                string emailKey = NormalizeEmailKey(row.Identifier);
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
            }
            catch (Exception ex)
            {
                Log("删除失败：" + row.Identifier + " " + ex.Message);
                return false;
            }
        }

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

        private void CancelBatch_Click(object sender, RoutedEventArgs e)
        {
            if (runningProcess == null || runningProcess.HasExited)
            {
                Log("当前没有运行中的批次。");
                return;
            }
            try
            {
                runningProcess.Kill(true);
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
