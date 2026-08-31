using System.Text.Json;

namespace SmsWorkbench
{
    public interface IProtocolPaymentService
    {
        ProtocolPaymentPreferences LoadPreferences();
        void SavePreferences(ProtocolPaymentPreferences preferences);
        PaymentBatchProxyConfiguration LoadProxyConfiguration(string paymentMethod);
        SettingsSaveResult SaveProxyConfiguration(string paymentMethod, PaymentBatchProxyConfiguration configuration);
        Task<string> TestProxiesAsync(
            string paymentMethod,
            PaymentBatchProxyConfiguration configuration,
            CancellationToken cancellationToken);
        Task<ProtocolPaymentRunResult> RunAsync(
            ProtocolPaymentRequest request,
            IProgress<BackendOutputLine> progress,
            CancellationToken cancellationToken);
    }

    public sealed class ProtocolPaymentService : IProtocolPaymentService
    {
        private static readonly JsonSerializerOptions JsonOptions = new()
        {
            PropertyNameCaseInsensitive = true,
            WriteIndented = true
        };

        private readonly IApplicationPaths _paths;
        private readonly IBackendTaskCoordinator _backendTasks;
        private readonly IPaymentBatchService _paymentBatchService;
        private readonly ISettingsService _settings;

        public ProtocolPaymentService(
            IApplicationPaths paths,
            IBackendTaskCoordinator backendTasks,
            IPaymentBatchService paymentBatchService,
            ISettingsService settings)
        {
            _paths = paths;
            _backendTasks = backendTasks;
            _paymentBatchService = paymentBatchService;
            _settings = settings;
        }

        public ProtocolPaymentPreferences LoadPreferences()
        {
            try
            {
                string path = PreferencesPath();
                if (File.Exists(path))
                {
                    ProtocolPaymentHistoryFile saved = JsonSerializer.Deserialize<ProtocolPaymentHistoryFile>(
                        File.ReadAllText(path, Encoding.UTF8), JsonOptions);
                    if (saved?.Last != null)
                        return saved.Last;
                }
            }
            catch
            {
            }

            return new ProtocolPaymentPreferences
            {
                CheckoutCountry = First(_settings.GetString("paypal.stage_proxy_countries.checkout"), "US"),
                ApproveCountry = First(_settings.GetString("paypal.stage_proxy_countries.approve"), "TR"),
                UpdateCountry = First(_settings.GetString("paypal.stage_proxy_countries.promotion"), "TR"),
                TargetCountry = First(_settings.GetString("paypal.target_country"), "US")
            };
        }

        public void SavePreferences(ProtocolPaymentPreferences preferences)
        {
            ArgumentNullException.ThrowIfNull(preferences);
            string path = PreferencesPath();
            Directory.CreateDirectory(Path.GetDirectoryName(path) ?? _paths.RootDirectory);
            ProtocolPaymentHistoryFile saved = null;
            try
            {
                if (File.Exists(path))
                    saved = JsonSerializer.Deserialize<ProtocolPaymentHistoryFile>(File.ReadAllText(path, Encoding.UTF8), JsonOptions);
            }
            catch
            {
            }
            saved ??= new ProtocolPaymentHistoryFile();
            saved.History ??= new List<ProtocolPaymentHistoryEntry>();
            string signature = preferences.Signature();
            if (saved.History.Count == 0 || !string.Equals(saved.History[0].Signature, signature, StringComparison.Ordinal))
            {
                saved.History.Insert(0, new ProtocolPaymentHistoryEntry
                {
                    SavedAt = DateTimeOffset.Now.ToString("O", CultureInfo.InvariantCulture),
                    Signature = signature,
                    Selection = preferences
                });
            }
            saved.History = saved.History.Take(20).ToList();
            saved.Last = preferences;
            File.WriteAllText(path, JsonSerializer.Serialize(saved, JsonOptions), new UTF8Encoding(false));
        }

        public PaymentBatchProxyConfiguration LoadProxyConfiguration(string paymentMethod)
            => _paymentBatchService.LoadProxyConfiguration(paymentMethod);

        public SettingsSaveResult SaveProxyConfiguration(string paymentMethod, PaymentBatchProxyConfiguration configuration)
            => _paymentBatchService.SaveProxyConfiguration(paymentMethod, configuration);

        public async Task<string> TestProxiesAsync(
            string paymentMethod,
            PaymentBatchProxyConfiguration configuration,
            CancellationToken cancellationToken)
        {
            IReadOnlyList<string> arguments = ProtocolPaymentExecutionPlanner.CreateProxyTestArguments(
                paymentMethod,
                "",
                configuration.CheckoutProxyPool,
                configuration.ApproveProxyPool,
                configuration.CheckoutCountry,
                configuration.ApproveCountry,
                configuration.UpdateCountry);
            BackendCommandResult result = await _backendTasks.RunAsync(
                BackendCommand.Create("测试协议支付代理", arguments, 120000),
                cancellationToken: cancellationToken);
            if (result.TimedOut)
                throw new TimeoutException("代理探测超时（120s）");
            if (result.Payload.HasValue)
                return FormatProxyResult(result.Payload.Value.GetRawText());
            if (!string.IsNullOrWhiteSpace(result.StandardError))
                throw new InvalidOperationException(result.StandardError);
            throw new InvalidOperationException("后端未返回代理探测结果。");
        }

        public async Task<ProtocolPaymentRunResult> RunAsync(
            ProtocolPaymentRequest request,
            IProgress<BackendOutputLine> progress,
            CancellationToken cancellationToken)
        {
            ArgumentNullException.ThrowIfNull(request);
            string transientSessionFile = "";
            ProtocolPaymentExecutionPlan plan = null;
            try
            {
                string sessionFile = request.Account?.SessionFile ?? "";
                string accountEmail = request.Account?.Email ?? "";
                if (accountEmail.Length == 0)
                {
                    if (string.IsNullOrWhiteSpace(request.AccessToken))
                        return new ProtocolPaymentRunResult(new ProtocolPaymentResultPresentation("请输入 Access Token", "", ""));
                    transientSessionFile = Path.Combine(Path.GetTempPath(), "protocol_payment_at_" + Guid.NewGuid().ToString("N") + ".json");
                    File.WriteAllText(
                        transientSessionFile,
                        JsonSerializer.Serialize(new Dictionary<string, string> { ["access_token"] = request.AccessToken.Trim() }),
                        new UTF8Encoding(false));
                    sessionFile = transientSessionFile;
                }

                plan = ProtocolPaymentExecutionPlanner.Create(new ProtocolPaymentExecutionRequest(
                    request.PaymentMethod,
                    request.TargetCountry,
                    "",
                    request.CheckoutProxyPool,
                    request.ApproveProxyPool,
                    request.JitRefresh,
                    request.ProbeOnly,
                    request.RequireZero,
                    request.RequireBaToken,
                    request.BlikCode,
                    request.CheckoutCountry,
                    request.ApproveCountry,
                    request.UpdateCountry,
                    accountEmail,
                    sessionFile));

                int timeoutMs = BackendTimeoutMs(request.PaymentMethod);
                BackendCommandResult backend = await _backendTasks.RunAsync(
                    BackendCommand.Create(
                        plan.TaskName,
                        plan.Arguments,
                        timeoutMs,
                        new Dictionary<string, string> { ["SMSWORKBENCH_EVENTS"] = "1" }),
                    progress,
                    cancellationToken);
                BackendExecutionResult execution = BackendResultInterpreter.Interpret(backend, plan.TaskName, timeoutMs / 1000);
                if (!execution.IsSuccess || execution.State != "completed")
                {
                    ProtocolPaymentResultPresentation failed = execution.State switch
                    {
                        "timed_out" => ProtocolPaymentResultPresenter.Aborted(plan, "timed_out"),
                        "cancelled" => ProtocolPaymentResultPresenter.Aborted(plan, "cancelled"),
                        _ when execution.Payload.HasValue => ProtocolPaymentResultPresenter.Parse(execution.Payload.Value.GetRawText()),
                        _ => ProtocolPaymentResultPresenter.Parse(execution.DisplayText)
                    };
                    return new ProtocolPaymentRunResult(failed);
                }
                string raw = execution.Payload.HasValue ? execution.Payload.Value.GetRawText() : execution.DisplayText;
                return new ProtocolPaymentRunResult(ProtocolPaymentResultPresenter.Parse(raw));
            }
            catch (OperationCanceledException)
            {
                return new ProtocolPaymentRunResult(ProtocolPaymentResultPresenter.Aborted(plan, "cancelled"));
            }
            catch (TimeoutException)
            {
                return new ProtocolPaymentRunResult(ProtocolPaymentResultPresenter.Aborted(plan, "timed_out"));
            }
            catch (Exception exception)
            {
                return new ProtocolPaymentRunResult(
                    new ProtocolPaymentResultPresentation("[异常] " + SensitiveDataSanitizer.Redact(exception.Message), "", ""),
                    SensitiveDataSanitizer.Redact(exception.Message));
            }
            finally
            {
                TryDelete(transientSessionFile);
            }
        }

        private int BackendTimeoutMs(string paymentMethod)
        {
            int seconds = 900;
            if (int.TryParse(_settings.GetString("protocol_payments.timeout_seconds"), out int configured))
                seconds = configured;
            string methodPath = "protocol_payments.methods." + PaymentMethods.Normalize(paymentMethod) + ".timeout_seconds";
            if (int.TryParse(_settings.GetString(methodPath), out int methodConfigured))
                seconds = methodConfigured;
            return (Math.Max(30, Math.Min(3600, seconds)) + 30) * 1000;
        }

        private static string FormatProxyResult(string raw)
        {
            ProxyTestResult result = BackendResultInterpreter.ParseProxyTestResult(raw);
            var lines = new List<string>
            {
                result.AllOk ? "[成功] 代理出口符合选择" : "[失败] 存在不可用或地区不匹配的代理"
            };
            foreach (ProxyTestStageResult stage in result.Stages)
            {
                string detail = $"{stage.Stage}: {stage.Ip} / {stage.ActualCountry} (目标 {stage.ExpectedCountry})";
                if (stage.Error.Length > 0)
                    detail += " - " + stage.Error;
                lines.Add(detail);
            }
            return string.Join(Environment.NewLine, lines);
        }

        private string PreferencesPath() => Path.Combine(_paths.RootDirectory, "runtime", "protocol_payment_history.json");
        private static string First(string value, string fallback) => string.IsNullOrWhiteSpace(value) ? fallback : value.Trim();

        private static void TryDelete(string path)
        {
            try
            {
                if (path.Length > 0 && File.Exists(path))
                    File.Delete(path);
            }
            catch
            {
            }
        }
    }
}
