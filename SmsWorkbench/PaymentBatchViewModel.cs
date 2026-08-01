using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using System.Collections.ObjectModel;
using System.Text.Json;

namespace SmsWorkbench
{
    public sealed partial class PaymentBatchViewModel : ObservableObject
    {
        private readonly IPaymentBatchService _paymentBatchService;
        private readonly IFileLauncher _fileLauncher;
        private readonly PaymentBatchAccount[] _accounts;
        private string _automaticBatchId;

        [ObservableProperty] private PaymentMethodOption selectedMethod;
        [ObservableProperty] private int workers = 2;
        [ObservableProperty] private int retries = 1;
        [ObservableProperty] private string canaryText = "0";
        [ObservableProperty] private string batchId = "";
        [ObservableProperty] private string proxy = "";
        [ObservableProperty] private bool jitRefresh = true;
        [ObservableProperty] private bool probeOnly;
        [ObservableProperty] private bool requireZero = true;
        [ObservableProperty] private PaymentMatrixRow selectedMatrixRow;
        [ObservableProperty] private string status = "就绪";
        [ObservableProperty] private string reportPath = "";
        [ObservableProperty] private bool isRunning;
        [ObservableProperty] private bool hasRun;

        public PaymentBatchViewModel(
            IPaymentBatchService paymentBatchService,
            IFileLauncher fileLauncher,
            IEnumerable<PaymentBatchAccount> accounts)
        {
            _paymentBatchService = paymentBatchService;
            _fileLauncher = fileLauncher;
            _accounts = (accounts ?? Array.Empty<PaymentBatchAccount>())
                .Where(account => !string.IsNullOrWhiteSpace(account.Email))
                .GroupBy(account => account.Email.Trim(), StringComparer.OrdinalIgnoreCase)
                .Select(group => group.First() with { Email = group.Key })
                .ToArray();
            PaymentMethodOptions = PaymentMethods.BatchOptions;
            WorkerOptions = Enumerable.Range(1, 10).ToArray();
            RetryOptions = new[] { 0, 1, 2 };
            selectedMethod = PaymentMethodOptions.First(option => option.Id == "momo");
            _automaticBatchId = CreateBatchId(selectedMethod.Id);
            batchId = _automaticBatchId;
            ReloadMatrix();
        }

        public IReadOnlyList<PaymentMethodOption> PaymentMethodOptions { get; }

        public IReadOnlyList<int> WorkerOptions { get; }

        public IReadOnlyList<int> RetryOptions { get; }

        public ObservableCollection<PaymentMatrixRow> MatrixRows { get; } = new();

        public ObservableCollection<PaymentBatchResultRow> Results { get; } = new();

        public string AccountSummary => $"账号 {_accounts.Length}  ·  AT 已获取 {_accounts.Count(account => account.HasAccessToken)}";

        public bool RequireZeroEnabled => !ProbeOnly;

        private bool CanRun() => !IsRunning && _accounts.Length > 0;

        private bool CanDeleteMatrixRow() => !IsRunning && SelectedMatrixRow != null && MatrixRows.Count > 1;

        private bool CanOpenReport() => !IsRunning && _fileLauncher.Exists(ReportPath);

        partial void OnSelectedMethodChanged(PaymentMethodOption value)
        {
            if (value == null) return;
            if (string.IsNullOrWhiteSpace(BatchId) || string.Equals(BatchId, _automaticBatchId, StringComparison.Ordinal))
            {
                _automaticBatchId = CreateBatchId(value.Id);
                BatchId = _automaticBatchId;
            }
            ReloadMatrix();
        }

        partial void OnProbeOnlyChanged(bool value) => OnPropertyChanged(nameof(RequireZeroEnabled));

        partial void OnSelectedMatrixRowChanged(PaymentMatrixRow value) => DeleteMatrixRowCommand.NotifyCanExecuteChanged();

        partial void OnReportPathChanged(string value) => OpenReportCommand.NotifyCanExecuteChanged();

        partial void OnIsRunningChanged(bool value)
        {
            RunCommand.NotifyCanExecuteChanged();
            DeleteMatrixRowCommand.NotifyCanExecuteChanged();
            OpenReportCommand.NotifyCanExecuteChanged();
        }

        [RelayCommand]
        private void AddMatrixRow()
        {
            MatrixRows.Add(_paymentBatchService.CreateDefaultMatrixRow(SelectedMethod?.Id ?? "paypal"));
            DeleteMatrixRowCommand.NotifyCanExecuteChanged();
        }

        [RelayCommand(CanExecute = nameof(CanDeleteMatrixRow))]
        private void DeleteMatrixRow()
        {
            if (SelectedMatrixRow == null || MatrixRows.Count <= 1) return;
            MatrixRows.Remove(SelectedMatrixRow);
            SelectedMatrixRow = null;
            DeleteMatrixRowCommand.NotifyCanExecuteChanged();
        }

        [RelayCommand(CanExecute = nameof(CanOpenReport))]
        private void OpenReport() => _fileLauncher.Open(ReportPath);

        [RelayCommand(IncludeCancelCommand = true, CanExecute = nameof(CanRun))]
        private async Task RunAsync(CancellationToken cancellationToken)
        {
            if (!TryCreateRequest(out PaymentBatchRequest request)) return;
            Results.Clear();
            ReportPath = "";
            Status = ProbeOnly ? "正在执行 JIT 探测与资格矩阵校验..." : "正在执行 JIT 探测与协议支付批次...";
            IsRunning = true;
            try
            {
                JsonElement report = await _paymentBatchService.RunAsync(request, cancellationToken);
                HasRun = true;
                PopulateResults(report);
                ReportPath = JsonString(report, "report_path");
                string error = JsonString(report, "error");
                Status = error.Length > 0 && !report.TryGetProperty("counts", out _)
                    ? "执行失败：" + error
                    : FormatSummary(report);
            }
            catch (OperationCanceledException)
            {
                Status = "已取消。";
            }
            catch (Exception exception)
            {
                Status = "执行失败：" + exception.Message;
            }
            finally
            {
                IsRunning = false;
            }
        }

        private bool TryCreateRequest(out PaymentBatchRequest request)
        {
            request = null;
            if (!int.TryParse(CanaryText.Trim(), out int canary) || canary < 0)
            {
                Status = "Canary 数量必须是非负整数。";
                return false;
            }
            string normalizedBatchId = Regex.Replace((BatchId ?? "").Trim(), @"[^A-Za-z0-9_.-]+", "_");
            if (normalizedBatchId.Length == 0)
            {
                Status = "请输入批次 ID。";
                return false;
            }
            if (MatrixRows.Any(cell => !cell.IsValid()))
            {
                Status = "矩阵国家代码必须为空或两位字母，样本数必须大于 0。";
                return false;
            }
            BatchId = normalizedBatchId;
            request = new PaymentBatchRequest(
                _accounts,
                SelectedMethod?.Id ?? "paypal",
                Workers,
                Retries,
                canary,
                normalizedBatchId,
                Proxy ?? "",
                JitRefresh,
                ProbeOnly,
                RequireZero,
                MatrixRows.ToArray());
            return true;
        }

        private void ReloadMatrix()
        {
            if (_paymentBatchService == null || SelectedMethod == null) return;
            MatrixRows.Clear();
            IReadOnlyList<PaymentMatrixRow> configured = _paymentBatchService.LoadMatrix(SelectedMethod.Id);
            foreach (PaymentMatrixRow row in configured.Count > 0
                ? configured
                : new[] { _paymentBatchService.CreateDefaultMatrixRow(SelectedMethod.Id) })
            {
                MatrixRows.Add(row);
            }
            DeleteMatrixRowCommand.NotifyCanExecuteChanged();
        }

        private void PopulateResults(JsonElement report)
        {
            if (!report.TryGetProperty("results", out JsonElement values) || values.ValueKind != JsonValueKind.Array) return;
            foreach (JsonElement row in values.EnumerateArray())
            {
                string eligibility = "未知";
                if (row.TryGetProperty("eligible", out JsonElement eligible)
                    && eligible.ValueKind is JsonValueKind.True or JsonValueKind.False)
                    eligibility = eligible.GetBoolean() ? "符合" : "不符合";
                string decision = JsonString(row, "decision");
                Results.Add(new PaymentBatchResultRow
                {
                    AccountRef = JsonString(row, "account_ref"),
                    MatrixCell = JsonString(row, "matrix_cell"),
                    AuthStatus = JsonBool(row, "authenticated") ? "200" : "失败",
                    RefreshStatus = JsonBool(row, "refreshed") ? "已刷新" : "未刷新",
                    Eligibility = eligibility,
                    Decision = decision.Length > 0 ? decision : JsonString(row, "error"),
                    Attempts = JsonInt(row, "attempts")
                });
            }
        }

        private static string FormatSummary(JsonElement report)
        {
            if (!report.TryGetProperty("counts", out JsonElement counts) || counts.ValueKind != JsonValueKind.Object)
                return "批次已结束，但未返回计数。";
            return $"请求 {JsonInt(counts, "requested")}  ·  AT 200 {JsonInt(counts, "authenticated")}"
                + $"  ·  JIT {JsonInt(counts, "refreshed")}  ·  资格 {JsonInt(counts, "eligible")}"
                + $"  ·  完成 {JsonInt(counts, "completed")}  ·  链接 {JsonInt(counts, "link_ready")}"
                + $"  ·  二维码 {JsonInt(counts, "qr_ready")}  ·  失败 {JsonInt(counts, "failed")}"
                + $"  ·  断点恢复 {JsonInt(report, "resumed")}";
        }

        private static string JsonString(JsonElement element, string name)
        {
            if (!element.TryGetProperty(name, out JsonElement value)) return "";
            return value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : value.ToString();
        }

        private static int JsonInt(JsonElement element, string name)
        {
            if (!element.TryGetProperty(name, out JsonElement value)) return 0;
            if (value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out int number)) return number;
            return int.TryParse(value.ToString(), out number) ? number : 0;
        }

        private static bool JsonBool(JsonElement element, string name)
            => element.TryGetProperty(name, out JsonElement value) && value.ValueKind == JsonValueKind.True;

        private static string CreateBatchId(string paymentMethod)
            => PaymentMethods.Normalize(paymentMethod) + "_" + DateTime.Now.ToString("yyyyMMdd_HHmmss", CultureInfo.InvariantCulture);
    }
}
