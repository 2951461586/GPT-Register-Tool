using SmsWorkbench.Localization;

namespace SmsWorkbench.Views
{
    public partial class MainWindow
    {
        private readonly AccountDeleteService accountDelete;
        private readonly BackendBatchCoordinator backendBatch;

        internal async Task RunStartupDoctorProbeAsync(CancellationToken ct = default)
        {
            if (!backendBatch.TryBeginDoctorProbe())
                return;
            try
            {
                var command = BackendCommand.Create("doctor", new[] { "--doctor", "--json" }, 90 * 1000);
                BackendCommandResult result = await backendClient.RunAsync(command, cancellationToken: ct).ConfigureAwait(true);
                if (!result.Payload.HasValue)
                {
                    Log("[doctor] 环境自检未返回结构化结果(退出码 " + result.ExitCode + ")");
                    return;
                }
                (List<string> fails, int warned) = BackendBatchCoordinator.ParseDoctorPayload(result.Payload.Value);
                if (fails.Count == 0)
                {
                    Log($"[doctor] 环境自检通过{(warned > 0 ? $"({warned} 项警告,详见设置与代理配置)" : "")}");
                    return;
                }
                var detail = string.Join("\n  - ", fails);
                Log("[doctor] 环境自检发现 " + fails.Count + " 项缺失依赖");
                await DialogFactory.ShowInfoAsync(
                    this,
                    Loc.T("环境自检"),
                    Loc.F("环境自检发现 {0} 项必需依赖缺失:\n  - {1}\n\n可先运行: python -m pip install -r requirements.txt -c constraints.txt\n或使用命令 python chatgpt_phone_reg.py --doctor 查看完整报告。",
                        fails.Count,
                        detail));
            }
            catch (Exception ex)
            {
                Log("[doctor] 环境自检失败: " + ex.Message);
                await DialogFactory.ShowInfoAsync(
                    this,
                    Loc.T("无法启动 Python 后端"),
                    Loc.F("{0}\n\n桌面端依赖 Python 3.10+ 与 requirements.txt/constraints.txt 中的依赖包。\n安装后在 设置 → 数据与文件 → 运行环境 配置解释器路径,再重启本程序。",
                        SensitiveDataSanitizer.Redact(ex.Message)));
            }
        }

        private void RunBackend(string taskName, List<string> args)
            => RunUiTask(() => RunBackendAsync(taskName, args, ct: _lifetimeCts.Token));

        private void RunBackend(BackendCommandPlan plan)
            => RunUiTask(() => RunBackendAsync(
                plan.TaskName,
                plan.Arguments.ToList(),
                timeoutMs: plan.TimeoutMilliseconds,
                resultKind: plan.ResultKind,
                temporaryFiles: plan.TempFiles,
                ct: _lifetimeCts.Token));

        private void RunAccountBatchBackend(BackendCommandPlan plan, string domain, int total)
            => RunUiTask(() => RunBackendAsync(
                plan.TaskName, plan.Arguments.ToList(), domain, total, plan.TimeoutMilliseconds,
                resultKind: plan.ResultKind, temporaryFiles: plan.TempFiles, ct: _lifetimeCts.Token));

        private Task RunBackendAsync(
            string taskName,
            List<string> args,
            string progressDomain = "",
            int progressTotal = 0,
            int? timeoutMs = null,
            BackendAccountResultKind resultKind = BackendAccountResultKind.None,
            IReadOnlyList<string>? temporaryFiles = null,
            CancellationToken ct = default)
            => TransientArtifactCleanup.RunWithCleanupAsync(
                temporaryFiles,
                () => RunBackendCoreAsync(
                    taskName, args, progressDomain, progressTotal, timeoutMs, resultKind, ct),
                DeleteTemporaryFiles);

        private async Task RunBackendCoreAsync(
            string taskName,
            List<string> args,
            string progressDomain,
            int progressTotal,
            int? timeoutMs,
            BackendAccountResultKind resultKind,
            CancellationToken ct)
        {
            if (backendTasks.IsRunning)
            {
                ShowThemedInfoDialog(Loc.T("运行中"), Loc.T("已有批次正在运行，请先取消或等待完成。"));
                return;
            }

            string safeArgs = BackendBatchCoordinator.FormatArgsForDisplay(args);
            TaskRow task = TaskList.StartBatch(taskName, safeArgs);
            ScrollTaskGridToBottom();
            DateTime started = DateTime.Now;
            AccountBatchProgressTracker? accountProgress = string.IsNullOrWhiteSpace(progressDomain)
                ? null
                : new AccountBatchProgressTracker(progressDomain, progressTotal);
            AccountBatchProgressDialog? progressDialog = accountProgress == null
                ? null
                : new AccountBatchProgressDialog(this, taskName, progressTotal, () => backendTasks.Cancel());
            progressDialog?.Show();

            var backendRun = new BackendRunService();
            string commandId = Guid.NewGuid().ToString("N");

            void HandleProgressEvent(BackendProgressEvent progressEvent)
            {
                LogBackendProgress(taskName, progressEvent);
                Metrics.Record(progressEvent);
                if (progressEvent.Domain == "registration"
                    && (progressEvent.Stage == "registration_status_changed"
                        || progressEvent.Stage == "mailboxes_skipped"))
                {
                    RefreshPoolsThrottled(preserveView: true);
                }
                if (accountProgress != null
                    && string.Equals(progressEvent.Domain, accountProgress.Domain, StringComparison.OrdinalIgnoreCase))
                {
                    if (accountProgress.Update(progressEvent))
                        RefreshPoolsThrottled(preserveView: true);
                    progressDialog?.Update(
                        accountProgress.Completed,
                        accountProgress.Total,
                        progressEvent.AccountRef,
                        progressEvent.Detail);
                }
                task.Info = progressEvent.Detail.Length > 0
                    ? $"{progressEvent.Stage}: {progressEvent.Detail}"
                    : progressEvent.Stage;
            }

            var progress = new Progress<BackendOutputLine>(
                line => backendRun.Feed(line, HandleProgressEvent, UiLog, RefreshPoolsAfterHotPersistence));
            string activeKey = string.Empty;
            try
            {
                activeKey = TakePendingNavKey();
                if (activeKey.Length > 0) ActiveNavKey = activeKey;
                logger?.Information("启动后端任务 task={TaskName} command_id={CommandId} args={Args}", taskName, commandId, safeArgs);
                Log(BackendLogPresenter.TaskStartLine(taskName));
                StatusBar.Text = taskName + " 运行中";
                BackendCommandResult result = await backendTasks.RunAsync(
                    BackendCommand.Create(
                        taskName,
                        args,
                        timeoutMs ?? BackendBatchCoordinator.BackendTaskTimeoutMs,
                        BackendBatchCoordinator.CreateTaskEnvironment(commandId, taskName)),
                    progress,
                    ct);

                BackendExecutionResult interpreted = BackendResultInterpreter.Interpret(
                    result, taskName, (timeoutMs ?? BackendBatchCoordinator.BackendTaskTimeoutMs) / 1000);

                if (result.TimedOut && resultKind == BackendAccountResultKind.Liveness)
                {
                    string snapshot = BackendBatchCoordinator.TryReadLatestLivenessSnapshot(rootDir);
                    if (snapshot.Length > 0)
                        backendRun.Capture(snapshot);
                }

                string batchSummary = BackendResultInterpreter.BatchSummaryLabel(
                    interpreted.Payload,
                    missingKey => Serilog.Log.Warning(
                        "Registration batch summary is missing required key {Key}", missingKey));
                BackendRunOutcome outcome = BackendRunService.Describe(interpreted, batchSummary, started, DateTime.Now);
                task.Status = outcome.Status;
                if (outcome.Info != null) task.Info = outcome.Info;
                task.Cost = outcome.CostSeconds;
                task.DoneAt = outcome.DoneAt;
                StatusBar.Text = taskName + " 已结束";
                RefreshPools();
                ScrollTaskGridToBottom();
                if (BackendResultInterpreter.HasAccountResults(resultKind))
                {
                    ShowAccountScanResultDialog(backendRun.CapturedOutput, resultKind);
                }
            }
            catch (OperationCanceledException)
            {
                task.Status = "已取消";
                task.DoneAt = SafeTime(DateTime.Now);
                StatusBar.Text = taskName + " 已取消";
            }
            catch (BackendTaskAlreadyRunningException)
            {
                task.Status = "未启动";
                task.DoneAt = SafeTime(DateTime.Now);
                StatusBar.Text = taskName + " 未启动";
                ShowThemedInfoDialog(Loc.T("运行中"), Loc.T("已有批次正在运行，请先取消或等待完成。"));
            }
            catch (Exception ex)
            {
                task.Status = "启动失败";
                Log("启动失败：" + ex.Message);
            }
            finally
            {
                ClearActiveNavKey(activeKey);
                progressDialog?.Close();
            }
        }

        private void DeleteTemporaryFiles(IReadOnlyList<string>? paths)
        {
            if (paths == null)
                return;
            foreach (string path in paths)
            {
                if (!string.IsNullOrWhiteSpace(path))
                    TryDeleteFile(path);
            }
        }

        private Task<string> RunBackendWithResultAsync(string taskName, List<string> args, int timeoutMs = 120000, CancellationToken ct = default)
            => backendBatch.RunForResultAsync(backendTasks, logger, Log, UiLog, taskName, args, timeoutMs, ct);

        private void LogBackendProgress(string taskName, BackendProgressEvent progressEvent)
        {
            logger?.Information(
                "后端进度 task={TaskName} command_id={CommandId} account_ref={AccountRef} stage={Stage} status={Status} failure_class={FailureClass} detail={Detail}",
                taskName,
                progressEvent.CommandId,
                progressEvent.AccountRef,
                progressEvent.Stage,
                progressEvent.Status,
                progressEvent.FailureClass,
                SensitiveDataSanitizer.Redact(progressEvent.Detail));
        }

        private void RefreshPoolsAfterHotPersistence(string line)
        {
            if (!BackendBatchCoordinator.LineHasSavedSessionMarker(line))
                return;
            RefreshPoolsThrottled(preserveView: false);
        }

        private void RefreshPoolsThrottled(bool preserveView)
        {
            if (!backendBatch.TryClaimHotRefreshSlot(DateTime.UtcNow))
                return;
            if (preserveView)
                RefreshPoolsPreservingView();
            else
                RefreshPools();
        }

        private void ScrollTaskGridToBottom() => consoleControl.ScrollToLast();

        private void DeleteSelected()
            => RunUiTask(() => DeleteSelectedAsync(_lifetimeCts.Token));

        private async Task DeleteSelectedAsync(CancellationToken ct = default)
        {
            var selected = SelectedEmailRowsOrNotify(Loc.T("删除"));
            if (selected.Count == 0) return;
            if (!await ShowDeleteConfirmDialog(selected.Count)) return;
            try
            {
                AccountDeleteResult result = await accountDelete
                    .DeleteAsync(selected.Select(row => NormalizeEmailKey(row.Identifier)).ToList(), ct)
                    .ConfigureAwait(true);
                if (result.Failed > 0)
                {
                    await DialogFactory.ShowInfoAsync(
                        this,
                        Loc.T("删除未完成"),
                        Loc.F("{0} 条记录未能完整删除。请查看运行日志。", result.Failed));
                }
            }
            catch (Exception ex)
            {
                Log("批量删除失败：" + SensitiveDataSanitizer.Redact(ex.Message));
                await DialogFactory.ShowInfoAsync(this, Loc.T("删除失败"), Loc.T("批量删除未完成，请查看运行日志。"));
            }
            finally
            {
                RefreshPools();
            }
        }

        private Task<bool> ShowDeleteConfirmDialog(int count)
            => DialogFactory.ShowConfirmAsync(
                this,
                Loc.F("删除选中的 {0} 条记录？", count),
                Loc.T("将同步清理本地邮箱池、SQLite 索引和匹配的 session 文件。此操作不可撤销。"),
                Loc.T("删除"),
                isDanger: true);

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

        private void CancelBatch()
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
    }
}
