namespace SmsWorkbench.Services
{
    /// <summary>
    /// Window-independent coordinator for backend batch runs: doctor report parsing,
    /// command environment construction, hot-persistence refresh throttling, liveness
    /// crash-snapshot recovery, and single-result backend execution.
    /// </summary>
    public sealed class BackendBatchCoordinator
    {
        public const int BackendTaskTimeoutMs = 12 * 60 * 60 * 1000;
        public const int BackendTaskTimeoutSeconds = 12 * 60 * 60;
        public const int HotRefreshMinIntervalMs = 750;

        private bool doctorProbeStarted;
        private DateTime lastHotPersistenceRefreshUtc = DateTime.MinValue;

        public bool TryBeginDoctorProbe()
        {
            if (doctorProbeStarted)
            {
                return false;
            }
            doctorProbeStarted = true;
            return true;
        }

        public static (List<string> Fails, int WarnedCount) ParseDoctorPayload(JsonElement payload)
        {
            var fails = new List<string>();
            int warned = 0;
            if (!payload.TryGetProperty("checks", out JsonElement checks) || checks.ValueKind != JsonValueKind.Array)
            {
                return (fails, warned);
            }

            foreach (JsonElement check in checks.EnumerateArray())
            {
                string status = check.TryGetProperty("status", out JsonElement statusElement) ? statusElement.GetString() ?? "" : "";
                string name = check.TryGetProperty("name", out JsonElement nameElement) ? nameElement.GetString() ?? "" : "";
                string hint = check.TryGetProperty("hint", out JsonElement hintElement) ? hintElement.GetString() ?? "" : "";
                if (status == "fail")
                {
                    fails.Add(string.IsNullOrEmpty(hint) ? name : $"{name}: {hint}");
                }
                else if (status == "warn")
                {
                    warned++;
                }
            }
            return (fails, warned);
        }

        public static Dictionary<string, string> CreateTaskEnvironment(string commandId, string taskName)
        {
            return new Dictionary<string, string>
            {
                ["SMSWORKBENCH_EVENTS"] = "1",
                ["SMS_TOOL_COMMAND_ID"] = commandId,
                ["SMS_TOOL_TASK_NAME"] = taskName,
                ["SMS_TOOL_EVENT_SOURCE"] = "wpf",
            };
        }

        public static string FormatArgsForDisplay(IReadOnlyList<string> args)
        {
            return SensitiveDataSanitizer.RedactArguments(args);
        }

        public static bool LineHasSavedSessionMarker(string? line)
        {
            return !string.IsNullOrWhiteSpace(line)
                && line.Contains(BackendTextMarkers.SavedSession, StringComparison.OrdinalIgnoreCase);
        }

        public bool TryClaimHotRefreshSlot(DateTime nowUtc)
        {
            if ((nowUtc - lastHotPersistenceRefreshUtc).TotalMilliseconds < HotRefreshMinIntervalMs)
            {
                return false;
            }
            lastHotPersistenceRefreshUtc = nowUtc;
            return true;
        }

        public static string TryReadLatestLivenessSnapshot(string rootDir)
        {
            try
            {
                string directory = Path.Combine(rootDir, "runtime", "account_liveness_batches");
                if (!Directory.Exists(directory)) return "";
                string? latest = Directory.GetFiles(directory, "*.json")
                    .OrderByDescending(File.GetLastWriteTimeUtc)
                    .FirstOrDefault();
                if (string.IsNullOrWhiteSpace(latest)) return "";
                using JsonDocument doc = JsonDocument.Parse(File.ReadAllText(latest));
                JsonElement root = doc.RootElement;
                if (!root.TryGetProperty("results", out _) || !root.TryGetProperty("total", out _))
                {
                    return "";
                }
                bool terminal = root.TryGetProperty("terminal", out JsonElement terminalEl)
                    && terminalEl.ValueKind == JsonValueKind.True;
                if (!terminal && File.GetLastWriteTimeUtc(latest) < DateTime.UtcNow.AddMinutes(-10))
                {
                    return "";
                }
                return root.GetRawText();
            }
            catch
            {
                return "";
            }
        }

        public async Task<string> RunForResultAsync(
            IBackendTaskCoordinator backendTasks,
            Serilog.ILogger? logger,
            Action<string> log,
            Action<string> uiLog,
            string taskName,
            List<string> args,
            int timeoutMs = 120000,
            CancellationToken ct = default)
        {
            string commandId = Guid.NewGuid().ToString("N");
            logger?.Information(
                "启动后端任务 task={TaskName} command_id={CommandId} args={Args}",
                taskName,
                commandId,
                FormatArgsForDisplay(args));
            log(BackendLogPresenter.TaskStartLine(taskName));
            var logFolder = new BackendLogFolder();
            var progress = new Progress<BackendOutputLine>(line =>
            {
                if (BackendProgressEventParser.TryParse(line.Text, out BackendProgressEvent? progressEvent))
                {
                    string? stageLine = BackendLogPresenter.ProgressEventLine(progressEvent);
                    if (stageLine != null)
                    {
                        uiLog(stageLine);
                    }
                    return;
                }
                foreach (string display in logFolder.Feed(line.Text))
                {
                    uiLog(display);
                }
            });
            return await backendTasks.RunForResultAsync(
                BackendCommand.Create(
                    taskName,
                    args,
                    timeoutMs,
                    CreateTaskEnvironment(commandId, taskName)),
                progress,
                ct).ConfigureAwait(true);
        }
    }
}
