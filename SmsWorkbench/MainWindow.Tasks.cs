namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Backend process, task list, deletion and cancellation actions.
        //
        // CLI argument construction is delegated to BackendCommandPlanner;
        // backend JSON business interpretation is delegated to
        // BackendResultInterpreter.

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

            var plan = BackendCommandPlanner.CreateRerunFailedRegistration(
                mailboxArg,
                tempFile,
                mailboxCount,
                GetRegistrationProxyPool());
            RunBackend(plan.TaskName, plan.Arguments.ToList());
        }

        private void RebuildSqlite_Click(object sender, RoutedEventArgs e)
        {
            var plan = BackendCommandPlanner.CreateRebuildSqlite();
            RunBackend(plan.TaskName, plan.Arguments.ToList());
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

                // Use BackendResultInterpreter to normalize the outcome
                BackendExecutionResult interpreted = BackendResultInterpreter.Interpret(
                    result, taskName, 12 * 60 * 60);

                task.Status = interpreted.IsSuccess ? "完成" : "失败";
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
                var plan = BackendCommandPlanner.CreateDeleteAccount(emailKey);
                BackendCommandResult backend = await backendTasks.RunAsync(
                    BackendCommand.Create(plan.TaskName, plan.Arguments.ToList(), plan.TimeoutMilliseconds ?? 120000));
                if (backend.ExitCode != 0 || !backend.Payload.HasValue)
                {
                    Log("删除失败：" + SensitiveDataSanitizer.Redact(emailKey));
                    return false;
                }
                Log("删除账号完成：" + SensitiveDataSanitizer.Redact(emailKey));
                return true;
            }
            catch (Exception ex)
            {
                Log("删除失败：" + SensitiveDataSanitizer.Redact(row.Identifier) + " " + SensitiveDataSanitizer.Redact(ex.Message));
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