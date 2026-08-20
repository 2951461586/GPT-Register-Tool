using System.Text.Json;

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Backend process, task list, deletion and cancellation actions.
        //
        // CLI argument construction is delegated to BackendCommandPlanner;
        // backend JSON business interpretation is delegated to
        // BackendResultInterpreter.

        private bool doctorProbeStarted;

        // Long-running backend tasks (registration / payment batches) share one
        // timeout budget; keep it a named constant instead of inline math.
        private const int BackendTaskTimeoutMs = 12 * 60 * 60 * 1000;
        private const int BackendTaskTimeoutSeconds = 12 * 60 * 60;
        private DateTime lastHotPersistenceRefreshUtc = DateTime.MinValue;

        /// <summary>
        /// One-shot background environment probe (`python -m sms_tool --doctor --json`)
        /// run straight through the backend client so the single-active-task
        /// invariant is untouched. Surfaces missing interpreter/dependencies
        /// with fix hints instead of letting them surface as per-task failures.
        /// </summary>
        internal async Task RunStartupDoctorProbeAsync()
        {
            if (doctorProbeStarted)
                return;
            doctorProbeStarted = true;
            try
            {
                var command = BackendCommand.Create("doctor", new[] { "--doctor", "--json" }, 90 * 1000);
                BackendCommandResult result = await backendClient.RunAsync(command).ConfigureAwait(true);
                if (!result.Payload.HasValue)
                {
                    Log("[doctor] 环境自检未返回结构化结果(退出码 " + result.ExitCode + ")");
                    return;
                }
                var fails = new List<string>();
                int warned = 0;
                foreach (JsonElement check in result.Payload.Value.GetProperty("checks").EnumerateArray())
                {
                    string status = check.TryGetProperty("status", out JsonElement statusElement) ? statusElement.GetString() : "";
                    string name = check.TryGetProperty("name", out JsonElement nameElement) ? nameElement.GetString() : "";
                    string hint = check.TryGetProperty("hint", out JsonElement hintElement) ? hintElement.GetString() : "";
                    if (status == "fail")
                        fails.Add(string.IsNullOrEmpty(hint) ? name : $"{name}: {hint}");
                    else if (status == "warn")
                        warned++;
                }
                if (fails.Count == 0)
                {
                    Log($"[doctor] 环境自检通过{(warned > 0 ? $"({warned} 项警告,详见设置与代理配置)" : "")}");
                    return;
                }
                var detail = string.Join("\n  - ", fails);
                Log("[doctor] 环境自检发现 " + fails.Count + " 项缺失依赖");
                MessageBox.Show(
                    this,
                    $"环境自检发现 {fails.Count} 项必需依赖缺失:\n  - {detail}\n\n" +
                    "可先运行: python -m pip install -r requirements.txt\n" +
                    "或使用命令 python chatgpt_phone_reg.py --doctor 查看完整报告。",
                    "环境自检",
                    MessageBoxButton.OK,
                    MessageBoxImage.Warning);
            }
            catch (Exception ex)
            {
                Log("[doctor] 环境自检失败: " + ex.Message);
                MessageBox.Show(
                    this,
                    ex.Message + "\n\n桌面端依赖 Python 3.10+ 与 requirements.txt 中的依赖包。" +
                    "\n安装后在 设置 → 数据与文件 → 运行环境 配置解释器路径,再重启本程序。",
                    "无法启动 Python 后端",
                    MessageBoxButton.OK,
                    MessageBoxImage.Error);
            }
        }

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

        private void RunBackend(string taskName, List<string> args)
            => RunUiTask(() => RunBackendAsync(taskName, args));

        private void RunAccountBatchBackend(string taskName, List<string> args, string domain, int total)
            => RunUiTask(() => RunBackendAsync(taskName, args, domain, total));

        private async Task RunBackendAsync(string taskName, List<string> args, string progressDomain = "", int progressTotal = 0)
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
            AccountBatchProgressTracker accountProgress = string.IsNullOrWhiteSpace(progressDomain)
                ? null
                : new AccountBatchProgressTracker(progressDomain, progressTotal);
            AccountBatchProgressDialog progressDialog = accountProgress == null
                ? null
                : new AccountBatchProgressDialog(this, taskName, progressTotal, () => backendTasks.Cancel());
            progressDialog?.Show();

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
                if (BackendProgressEventParser.TryParse(line.Text, out BackendProgressEvent progressEvent))
                {
                    if (accountProgress != null
                        && string.Equals(progressEvent.Domain, accountProgress.Domain, StringComparison.OrdinalIgnoreCase))
                    {
                        accountProgress.Update(progressEvent);
                        progressDialog?.Update(
                            accountProgress.Completed,
                            accountProgress.Total,
                            progressEvent.AccountRef,
                            progressEvent.Detail);
                    }
                    task.Info = progressEvent.Detail.Length > 0
                        ? $"{progressEvent.Stage}: {progressEvent.Detail}"
                        : progressEvent.Stage;
                    return;
                }
                CaptureBackendLine(line.Text);
                UiLog(line.Text);
                RefreshPoolsAfterHotPersistence(line.Text);
            });
            try
            {
                Log("启动：python " + safeArgs);
                StatusText = taskName + " 运行中";
                BackendCommandResult result = await backendTasks.RunAsync(
                    BackendCommand.Create(
                        taskName,
                        args,
                        BackendTaskTimeoutMs,
                        new Dictionary<string, string> { ["SMSWORKBENCH_EVENTS"] = "1" }),
                    progress);

                // Use BackendResultInterpreter to normalize the outcome
                BackendExecutionResult interpreted = BackendResultInterpreter.Interpret(
                    result, taskName, BackendTaskTimeoutSeconds);

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
            finally
            {
                progressDialog?.Close();
            }
        }

        private async Task<string> RunBackendWithResultAsync(string taskName, List<string> args, int timeoutMs = 120000)
        {
            Log("启动：python " + FormatBackendArgsForDisplay(args));
            return await backendTasks.RunForResultAsync(
                BackendCommand.Create(taskName, args, timeoutMs));
        }

        private static string FormatBackendArgsForDisplay(List<string> args)
        {
            return SensitiveDataSanitizer.RedactArguments(args);
        }

        private void RefreshPoolsAfterHotPersistence(string line)
        {
            if (string.IsNullOrWhiteSpace(line)
                || !line.Contains("Saved session:", StringComparison.OrdinalIgnoreCase))
                return;

            DateTime now = DateTime.UtcNow;
            if ((now - lastHotPersistenceRefreshUtc).TotalMilliseconds < 750)
                return;
            lastHotPersistenceRefreshUtc = now;
            RefreshPools();
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

        private void DeleteSelected_Click(object sender, RoutedEventArgs e)
            => RunUiTask(DeleteSelectedAsync);

        private async Task DeleteSelectedAsync()
        {
            var selected = SelectedEmailRowsOrNotify("删除");
            if (selected.Count == 0) return;
            if (!await ShowDeleteConfirmDialog(selected.Count)) return;
            BackendCommandPlan plan = null;
            try
            {
                plan = BackendCommandPlanner.CreateBatchDeleteAccounts(
                    selected.Select(row => NormalizeEmailKey(row.Identifier)).ToArray(),
                    workers: Math.Min(8, Math.Max(1, selected.Count)));
                BackendCommandResult backend = await backendTasks.RunAsync(
                    BackendCommand.Create(plan.TaskName, plan.Arguments.ToList(), plan.TimeoutMilliseconds ?? 120000));
                int failed = CountBatchDeleteFailures(backend, selected.Count);
                if (failed > 0)
                {
                    await DialogFactory.ShowInfoAsync(
                        this,
                        "删除未完成",
                        failed + " 条记录未能完整删除。请查看运行日志。");
                }
            }
            catch (Exception ex)
            {
                Log("批量删除失败：" + SensitiveDataSanitizer.Redact(ex.Message));
                await DialogFactory.ShowInfoAsync(this, "删除失败", "批量删除未完成，请查看运行日志。");
            }
            finally
            {
                if (plan != null)
                {
                    foreach (string path in plan.TempFiles)
                        TryDeleteFile(path);
                }
                RefreshPools();
            }
        }

        private static int CountBatchDeleteFailures(BackendCommandResult backend, int expected)
        {
            if (backend.ExitCode != 0 || !backend.Payload.HasValue)
                return expected;
            JsonElement payload = backend.Payload.Value;
            if (payload.TryGetProperty("failed", out JsonElement failed) && failed.ValueKind == JsonValueKind.Number)
                return Math.Max(0, failed.GetInt32());
            return payload.TryGetProperty("ok", out JsonElement ok) && ok.ValueKind == JsonValueKind.True
                ? 0
                : expected;
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
