using System.Text.Json;
using System.Text.Json.Nodes;

namespace SmsWorkbench
{
    public interface IPaymentBatchService
    {
        IReadOnlyList<PaymentMatrixRow> LoadMatrix(string paymentMethod);
        PaymentMatrixRow CreateDefaultMatrixRow(string paymentMethod);
        Task<JsonElement> RunAsync(PaymentBatchRequest request, CancellationToken cancellationToken);
    }

    public sealed class PaymentBatchService : IPaymentBatchService
    {
        private static readonly JsonSerializerOptions IndentedJson = new() { WriteIndented = true };
        private readonly IApplicationPaths _paths;
        private readonly IBackendClient _backendClient;

        public PaymentBatchService(IApplicationPaths paths, IBackendClient backendClient)
        {
            _paths = paths;
            _backendClient = backendClient;
        }

        public IReadOnlyList<PaymentMatrixRow> LoadMatrix(string paymentMethod)
        {
            var output = new List<PaymentMatrixRow>();
            try
            {
                JsonNode root = JsonNode.Parse(File.ReadAllText(Path.Combine(_paths.RootDirectory, "config.json"), Encoding.UTF8));
                JsonArray cells = root?["protocol_payments"]?["matrix"]?["cells"] as JsonArray;
                foreach (JsonNode node in cells ?? new JsonArray())
                {
                    if (node is not JsonObject cell) continue;
                    string configuredMethod = Text(cell, "payment_method");
                    if (configuredMethod.Length > 0
                        && PaymentMethods.Normalize(configuredMethod) != PaymentMethods.Normalize(paymentMethod))
                        continue;
                    output.Add(new PaymentMatrixRow
                    {
                        Name = First(Text(cell, "name"), "cell_" + (output.Count + 1)),
                        RegistrationCountry = Text(cell, "registration_country"),
                        CheckoutCountry = Text(cell, "checkout_country"),
                        PromotionCountry = Text(cell, "promotion_country"),
                        ProviderCountry = Text(cell, "provider_country"),
                        ApproveCountry = Text(cell, "approve_country"),
                        RedirectCountry = Text(cell, "redirect_country"),
                        Strategy = Text(cell, "strategy"),
                        SampleSize = int.TryParse(Text(cell, "sample_size"), out int sample) ? Math.Max(1, sample) : 1
                    });
                }
            }
            catch
            {
            }
            return output;
        }

        public PaymentMatrixRow CreateDefaultMatrixRow(string paymentMethod)
        {
            string normalized = PaymentMethods.Normalize(paymentMethod);
            string country = normalized == "momo" ? "VN" : normalized == "kakao" ? "KR" : "";
            return new PaymentMatrixRow
            {
                Name = country.Length > 0 ? country.ToLowerInvariant() + "_sticky" : "default",
                CheckoutCountry = country,
                PromotionCountry = normalized == "kakao" ? "VN" : country,
                ProviderCountry = country,
                ApproveCountry = country,
                RedirectCountry = country,
                Strategy = normalized == "momo" ? "custom_promo" : "",
                SampleSize = 5
            };
        }

        public async Task<JsonElement> RunAsync(PaymentBatchRequest request, CancellationToken cancellationToken)
        {
            string emailFile = Path.Combine(Path.GetTempPath(), "payment_batch_" + Guid.NewGuid().ToString("N") + ".txt");
            string matrixFile = Path.Combine(Path.GetTempPath(), "payment_matrix_" + Guid.NewGuid().ToString("N") + ".json");
            try
            {
                File.WriteAllLines(emailFile, request.Accounts.Select(account => account.Email), new UTF8Encoding(false));
                File.WriteAllText(matrixFile, SerializeMatrix(request.MatrixRows, request.PaymentMethod), new UTF8Encoding(false));
                var arguments = new List<string>
                {
                    "--desktop-ipc",
                    "--extract-payment-link",
                    "--payment-method", PaymentMethods.Normalize(request.PaymentMethod),
                    "--email-file", emailFile,
                    "--workers", request.Workers.ToString(CultureInfo.InvariantCulture),
                    "--payment-batch-id", request.BatchId,
                    "--payment-retries", request.Retries.ToString(CultureInfo.InvariantCulture),
                    "--payment-matrix", matrixFile,
                    "--refresh-timeout", "180"
                };
                if (!request.JitRefresh) arguments.Add("--no-jit-at-refresh");
                if (request.ProbeOnly) arguments.Add("--payment-probe-only");
                if (!request.RequireZero) arguments.Add("--no-require-zero");
                if (request.Canary > 0) arguments.AddRange(new[] { "--payment-canary", request.Canary.ToString(CultureInfo.InvariantCulture) });
                if (!string.IsNullOrWhiteSpace(request.Proxy)) arguments.AddRange(new[] { "--proxy", request.Proxy.Trim() });

                int waveSize = request.Canary > 0 ? Math.Min(request.Canary, request.Accounts.Count) : request.Accounts.Count;
                int waves = Math.Max(1, (int)Math.Ceiling(waveSize / (double)Math.Max(1, request.Workers)));
                long timeout = Math.Max(120000L, (long)GetMethodTimeoutMilliseconds(request.PaymentMethod) * waves);
                timeout = Math.Min(12L * 60 * 60 * 1000, timeout);
                BackendCommandResult result = await _backendClient.RunAsync(
                    BackendCommand.Create("批量协议支付", arguments, (int)timeout),
                    cancellationToken: cancellationToken);

                if (result.TimedOut)
                    throw new TimeoutException($"Backend execution timed out ({timeout / 1000}s)");
                if (result.Payload.HasValue)
                    return result.Payload.Value;
                if (!string.IsNullOrWhiteSpace(result.StandardError))
                    throw new InvalidOperationException(result.StandardError);
                throw new InvalidOperationException("后端未返回 SMSWORKBENCH IPC v1 结果。");
            }
            finally
            {
                TryDelete(emailFile);
                TryDelete(matrixFile);
            }
        }

        private int GetMethodTimeoutMilliseconds(string paymentMethod)
        {
            int seconds = 900;
            try
            {
                JsonNode root = JsonNode.Parse(File.ReadAllText(Path.Combine(_paths.RootDirectory, "config.json"), Encoding.UTF8));
                JsonNode protocol = root?["protocol_payments"];
                if (int.TryParse(protocol?["timeout_seconds"]?.ToString(), out int configured))
                    seconds = configured;
                JsonNode method = protocol?["methods"]?[PaymentMethods.Normalize(paymentMethod)];
                if (int.TryParse(method?["timeout_seconds"]?.ToString(), out int methodConfigured))
                    seconds = methodConfigured;
            }
            catch
            {
            }
            seconds = Math.Max(30, Math.Min(3600, seconds));
            return (seconds + 30) * 1000;
        }

        private static string SerializeMatrix(IEnumerable<PaymentMatrixRow> rows, string paymentMethod)
        {
            var cells = rows.Select(row => new
            {
                name = row.Name.Trim(),
                payment_method = PaymentMethods.Normalize(paymentMethod),
                registration_country = row.RegistrationCountry.Trim().ToUpperInvariant(),
                checkout_country = row.CheckoutCountry.Trim().ToUpperInvariant(),
                promotion_country = row.PromotionCountry.Trim().ToUpperInvariant(),
                provider_country = row.ProviderCountry.Trim().ToUpperInvariant(),
                approve_country = row.ApproveCountry.Trim().ToUpperInvariant(),
                redirect_country = row.RedirectCountry.Trim().ToUpperInvariant(),
                strategy = row.Strategy.Trim(),
                sample_size = Math.Max(1, row.SampleSize)
            });
            return JsonSerializer.Serialize(new { cells }, IndentedJson);
        }

        private static string Text(JsonObject value, string name) => value[name]?.ToString() ?? "";

        private static string First(string value, string fallback) => string.IsNullOrWhiteSpace(value) ? fallback : value;

        private static void TryDelete(string path)
        {
            try
            {
                if (File.Exists(path)) File.Delete(path);
            }
            catch
            {
            }
        }
    }
}
