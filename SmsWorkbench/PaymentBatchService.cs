using System.Text.Json;
using System.Text.Json.Nodes;

namespace SmsWorkbench
{
    public interface IPaymentBatchService
    {
        IReadOnlyList<PaymentMatrixRow> LoadMatrix(string paymentMethod);
        PaymentMatrixRow CreateDefaultMatrixRow(string paymentMethod);
        PaymentBatchProxyConfiguration LoadProxyConfiguration(string paymentMethod);
        SettingsSaveResult SaveProxyConfiguration(string paymentMethod, PaymentBatchProxyConfiguration configuration);
        Task<JsonElement> RunAsync(PaymentBatchRequest request, CancellationToken cancellationToken);
        Task<JsonElement> ProbeProxiesAsync(
            string paymentMethod,
            string checkoutProxyPool,
            string approveProxyPool,
            string checkoutCountry,
            string approveCountry,
            CancellationToken cancellationToken);
    }

    public interface IPaymentBatchProgressService
    {
        Task<JsonElement> RunAsync(
            PaymentBatchRequest request,
            IProgress<BackendOutputLine> progress,
            CancellationToken cancellationToken);
    }

    public sealed class PaymentBatchService : IPaymentBatchService, IPaymentBatchProgressService
    {
        private static readonly string[] ListSeparators = ["\r\n", "\n", ",", ";"];
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

        public PaymentBatchProxyConfiguration LoadProxyConfiguration(string paymentMethod)
        {
            string method = PaymentMethods.Normalize(paymentMethod);
            if (method.Length == 0)
                method = "paypal";

            string checkoutCountry = PaymentMethods.Find(method).DefaultCountry;
            string approveCountry = method == "gopay" ? "JP" : checkoutCountry;
            string updateCountry = PaymentMethods.DefaultUpdateCountry(method, approveCountry);

            try
            {
                JsonNode root = JsonNode.Parse(File.ReadAllText(Path.Combine(_paths.RootDirectory, "config.json"), Encoding.UTF8));
                JsonObject protocol = root?["protocol_payments"] as JsonObject;
                JsonObject methods = protocol?["methods"] as JsonObject;
                JsonObject legacy = root?[method] as JsonObject;
                JsonObject configured = methods?[method] as JsonObject;
                JsonObject namedPools = protocol?["proxy_pools"] as JsonObject;
                JsonObject routes = configured?["stage_routes"] as JsonObject;
                JsonObject stages = configured?["stage_proxies"] as JsonObject;
                JsonObject countries = configured?["stage_proxy_countries"] as JsonObject;
                JsonObject legacyStages = legacy?["stage_proxies"] as JsonObject;
                JsonObject legacyCountries = legacy?["stage_proxy_countries"] as JsonObject;

                string[] fallbackPool = NormalizePool(FirstPool(protocol?["proxy_pool"]));
                string checkoutPool = FirstPool(
                    NamedRoutePool(routes, "checkout", namedPools),
                    configured?["checkout_proxy_pool"],
                    configured?["checkout_proxy"],
                    stages?["checkout"],
                    legacy?["checkout_proxy_pool"],
                    legacy?["checkout_proxy"],
                    legacyStages?["checkout"],
                    configured?["proxy"],
                    legacy?["proxy"],
                    fallbackPool);
                string approvePool = FirstPool(
                    NamedRoutePool(routes, "approve", namedPools),
                    configured?["approve_proxy_pool"],
                    configured?["approve_proxy"],
                    stages?["approve"],
                    legacy?["approve_proxy_pool"],
                    legacy?["approve_proxy"],
                    legacyStages?["approve"],
                    configured?["proxy"],
                    legacy?["proxy"],
                    fallbackPool);

                checkoutCountry = First(
                    Text(countries, "checkout"),
                    Text(legacyCountries, "checkout"),
                    checkoutCountry).ToUpperInvariant();
                approveCountry = First(
                    Text(countries, "approve"),
                    Text(legacyCountries, "approve"),
                    approveCountry).ToUpperInvariant();
                updateCountry = First(
                    Text(countries, "promotion"),
                    Text(countries, "update"),
                    Text(legacyCountries, "promotion"),
                    Text(legacyCountries, "update"),
                    updateCountry).ToUpperInvariant();
                return new PaymentBatchProxyConfiguration(
                    NormalizePoolText(checkoutPool),
                    NormalizePoolText(approvePool),
                    checkoutCountry,
                    approveCountry,
                    updateCountry);
            }
            catch
            {
                return new PaymentBatchProxyConfiguration(
                    "",
                    "",
                    checkoutCountry,
                    approveCountry,
                    updateCountry);
            }
        }

        public SettingsSaveResult SaveProxyConfiguration(
            string paymentMethod,
            PaymentBatchProxyConfiguration configuration)
        {
            string method = PaymentMethods.Normalize(paymentMethod);
            if (method.Length == 0)
                return new SettingsSaveResult(false, "不支持的支付方式。");

            string checkoutCountry = (configuration?.CheckoutCountry ?? "").Trim().ToUpperInvariant();
            string approveCountry = (configuration?.ApproveCountry ?? "").Trim().ToUpperInvariant();
            string updateCountry = (configuration?.UpdateCountry ?? "").Trim().ToUpperInvariant();
            if (!ValidCountry(checkoutCountry) || !ValidCountry(approveCountry) || !ValidCountry(updateCountry))
                return new SettingsSaveResult(false, "代理出口国家必须为空或两位字母代码。");

            try
            {
                JsonObject root = ReadConfigRoot();
                JsonObject methods = EnsureObject(root, "protocol_payments", "methods");
                JsonObject proxyPools = EnsureObject(root, "protocol_payments", "proxy_pools");
                JsonObject methodConfig = EnsureObject(methods, method);
                string[] checkoutPool = NormalizePool(configuration?.CheckoutProxyPool);
                string[] approvePool = NormalizePool(configuration?.ApproveProxyPool);
                string checkoutPoolName = method + "_checkout";
                string approvePoolName = method + "_approve";
                SetArray(proxyPools, checkoutPoolName, checkoutPool);
                SetArray(proxyPools, approvePoolName, approvePool);
                SetArray(methodConfig, "checkout_proxy_pool", checkoutPool);
                SetArray(methodConfig, "approve_proxy_pool", approvePool);

                JsonObject routes = EnsureObject(methodConfig, "stage_routes");
                routes["checkout"] = new JsonObject { ["pool"] = checkoutPoolName, ["country"] = checkoutCountry };
                routes["approve"] = new JsonObject { ["pool"] = approvePoolName, ["country"] = approveCountry };
                routes["promotion"] = new JsonObject { ["pool"] = approvePoolName, ["country"] = updateCountry };

                JsonObject countries = EnsureObject(methodConfig, "stage_proxy_countries");
                if (checkoutCountry.Length > 0)
                    countries["checkout"] = checkoutCountry;
                else
                    countries.Remove("checkout");
                if (approveCountry.Length > 0)
                    countries["approve"] = approveCountry;
                else
                    countries.Remove("approve");
                if (updateCountry.Length > 0)
                    countries["promotion"] = updateCountry;
                else
                    countries.Remove("promotion");

                // Keep the first entry in the legacy singular keys for older
                // workers; the *_proxy_pool arrays remain authoritative.
                SetOptionalString(methodConfig, "checkout_proxy", checkoutPool.FirstOrDefault());
                SetOptionalString(methodConfig, "approve_proxy", approvePool.FirstOrDefault());
                WriteConfigRoot(root);
                return new SettingsSaveResult(true);
            }
            catch (Exception exception)
            {
                return new SettingsSaveResult(false, "代理配置保存失败：" + exception.Message);
            }
        }

        public PaymentMatrixRow CreateDefaultMatrixRow(string paymentMethod)
        {
            string normalized = PaymentMethods.Normalize(paymentMethod);
            string country = normalized switch
            {
                "gopay" => "ID",
                "gcash" or "grabpay" => "PH",
                "momo" => "VN",
                "kakao" => "KR",
                _ => ""
            };
            bool wallet = normalized is "gopay" or "gcash" or "grabpay";
            string approveCountry = normalized == "gopay" ? "JP" : country;
            return new PaymentMatrixRow
            {
                Name = country.Length > 0 ? country.ToLowerInvariant() + "_" + normalized : "default",
                // RegistrationCountry is an optional cohort filter. A GoPay
                // billing route does not imply that the account was registered
                // in Indonesia, so the default GoPay cohort stays neutral.
                RegistrationCountry = normalized == "gopay" ? "" : country,
                CheckoutCountry = country,
                PromotionCountry = normalized == "gopay"
                    ? "TH"
                    : PaymentMethods.DefaultUpdateCountry(normalized, country),
                ProviderCountry = country,
                ApproveCountry = approveCountry,
                RedirectCountry = country,
                Strategy = normalized == "momo" ? "custom_promo" : "",
                SampleSize = wallet ? 1 : 5
            };
        }

        public Task<JsonElement> RunAsync(PaymentBatchRequest request, CancellationToken cancellationToken)
            => RunAsync(request, null, cancellationToken);

        public async Task<JsonElement> RunAsync(
            PaymentBatchRequest request,
            IProgress<BackendOutputLine> progress,
            CancellationToken cancellationToken)
        {
            string emailFile = Path.Combine(Path.GetTempPath(), "payment_batch_" + Guid.NewGuid().ToString("N") + ".txt");
            string matrixFile = Path.Combine(Path.GetTempPath(), "payment_matrix_" + Guid.NewGuid().ToString("N") + ".json");
            string tokenFile = Path.Combine(Path.GetTempPath(), "payment_tokens_" + Guid.NewGuid().ToString("N") + ".json");
            try
            {
                File.WriteAllLines(emailFile, request.Accounts.Select(account => account.Email), new UTF8Encoding(false));
                File.WriteAllText(matrixFile, SerializeMatrix(request.MatrixRows, request.PaymentMethod), new UTF8Encoding(false));
                var tokenMap = request.Accounts
                    .Where(account => !string.IsNullOrWhiteSpace(account.AccessToken))
                    .ToDictionary(account => account.Email, account => account.AccessToken, StringComparer.OrdinalIgnoreCase);
                if (tokenMap.Count > 0)
                    File.WriteAllText(tokenFile, JsonSerializer.Serialize(tokenMap), new UTF8Encoding(false));
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
                if (tokenMap.Count > 0) arguments.AddRange(new[] { "--payment-token-map", tokenFile });
                if (!request.JitRefresh) arguments.Add("--no-jit-at-refresh");
                if (request.ProbeOnly) arguments.Add("--payment-probe-only");
                if (request.ResumeCheckpoint) arguments.Add("--payment-resume-checkpoint");
                if (!request.RequireZero) arguments.Add("--no-require-zero");
                if (request.Canary > 0) arguments.AddRange(new[] { "--payment-canary", request.Canary.ToString(CultureInfo.InvariantCulture) });
                AddPoolArgument(arguments, "--checkout-proxy-pool", request.CheckoutProxyPool);
                AddPoolArgument(arguments, "--approve-proxy-pool", request.ApproveProxyPool);
                AddCountryArgument(arguments, "--checkout-proxy-country", request.CheckoutCountry);
                AddCountryArgument(arguments, "--approve-proxy-country", request.ApproveCountry);
                AddCountryArgument(arguments, "--update-proxy-country", request.ApproveCountry);

                int waveSize = request.Canary > 0 ? Math.Min(request.Canary, request.Accounts.Count) : request.Accounts.Count;
                int waves = Math.Max(1, (int)Math.Ceiling(waveSize / (double)Math.Max(1, request.Workers)));
                long timeout = Math.Max(120000L, (long)GetMethodTimeoutMilliseconds(request.PaymentMethod) * waves);
                timeout = Math.Min(12L * 60 * 60 * 1000, timeout);
                BackendCommandResult result = await _backendClient.RunAsync(
                    BackendCommand.Create(
                        "批量协议支付",
                        arguments,
                        (int)timeout,
                        new Dictionary<string, string> { ["SMSWORKBENCH_EVENTS"] = "1" }),
                    progress,
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
                TryDelete(tokenFile);
            }
        }

        public async Task<JsonElement> ProbeProxiesAsync(
            string paymentMethod,
            string checkoutProxyPool,
            string approveProxyPool,
            string checkoutCountry,
            string approveCountry,
            CancellationToken cancellationToken)
        {
            var arguments = new List<string>
            {
                "--desktop-ipc",
                "--test-payment-proxies",
                "--payment-method", PaymentMethods.Normalize(paymentMethod),
            };
            AddPoolArgument(arguments, "--checkout-proxy-pool", checkoutProxyPool);
            AddPoolArgument(arguments, "--approve-proxy-pool", approveProxyPool);
            AddCountryArgument(arguments, "--checkout-proxy-country", checkoutCountry);
            AddCountryArgument(arguments, "--approve-proxy-country", approveCountry);
            AddCountryArgument(arguments, "--update-proxy-country", approveCountry);

            BackendCommandResult result = await _backendClient.RunAsync(
                BackendCommand.Create("测试代理", arguments, 120000),
                cancellationToken: cancellationToken);

            if (result.TimedOut)
                throw new TimeoutException("代理探测超时（120s）");
            if (result.Payload.HasValue)
                return result.Payload.Value;
            if (!string.IsNullOrWhiteSpace(result.StandardError))
                throw new InvalidOperationException(result.StandardError);
            throw new InvalidOperationException("后端未返回代理探测结果。");
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
            var cells = rows.Select(row =>
            {
                string checkout = row.CheckoutCountry.Trim().ToUpperInvariant();
                string promotion = First(
                    row.PromotionCountry,
                    PaymentMethods.Normalize(paymentMethod) == "gopay" ? "TH" : checkout).ToUpperInvariant();
                string provider = First(row.ProviderCountry, checkout).ToUpperInvariant();
                string approve = First(
                    row.ApproveCountry,
                    PaymentMethods.Normalize(paymentMethod) == "gopay" ? "JP" : checkout).ToUpperInvariant();
                string redirect = First(row.RedirectCountry, provider).ToUpperInvariant();
                return new
                {
                    name = row.Name.Trim(),
                    payment_method = PaymentMethods.Normalize(paymentMethod),
                    registration_country = row.RegistrationCountry.Trim().ToUpperInvariant(),
                    checkout_country = checkout,
                    // The two-pool UI controls proxy ownership, not the
                    // adapter's internal stage-country contract. Preserve
                    // promotion/provider/redirect values independently.
                    promotion_country = promotion,
                    provider_country = provider,
                    approve_country = approve,
                    redirect_country = redirect,
                    strategy = row.Strategy.Trim(),
                    sample_size = Math.Max(1, row.SampleSize)
                };
            });
            return JsonSerializer.Serialize(new { cells }, IndentedJson);
        }

        private static string Text(JsonObject value, string name) => value?[name]?.ToString() ?? "";

        private static string First(string value, string fallback) => string.IsNullOrWhiteSpace(value) ? fallback : value;

        private static string First(params string[] values)
            => values.FirstOrDefault(value => !string.IsNullOrWhiteSpace(value))?.Trim() ?? "";

        private static string FirstPool(params object[] values)
        {
            foreach (object value in values)
            {
                if (value is string[] array && array.Length > 0)
                    return string.Join(Environment.NewLine, array);
                string text = value switch
                {
                    JsonArray jsonArray => string.Join(Environment.NewLine, jsonArray.Select(item => item?.ToString() ?? "").Where(item => item.Length > 0)),
                    JsonNode node => node.ToString(),
                    _ => value?.ToString() ?? ""
                };
                string[] parsed = ParseList(text);
                if (parsed.Length > 0)
                    return string.Join(Environment.NewLine, parsed);
            }
            return "";
        }

        private static JsonNode NamedRoutePool(JsonObject routes, string stage, JsonObject namedPools)
        {
            JsonNode route = routes?[stage];
            string poolName = route is JsonObject routeObject
                ? routeObject["pool"]?.ToString() ?? ""
                : route?.ToString() ?? "";
            return poolName.Length > 0 ? namedPools?[poolName] : null;
        }

        private static string[] ParseList(string value)
            => (value ?? "")
                .Split(ListSeparators, StringSplitOptions.RemoveEmptyEntries)
                .Select(item => item.Trim())
                .Where(item => item.Length > 0)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToArray();

        private static string[] NormalizePool(string value)
            => ProxyInputNormalizer.NormalizeList(value);

        private static string NormalizePoolText(string value)
            => string.Join(Environment.NewLine, NormalizePool(value));

        private static bool ValidCountry(string value)
            => value.Length == 0 || Regex.IsMatch(value, "^[A-Z]{2}$", RegexOptions.CultureInvariant);

        private static JsonObject ReadConfigRoot(string path)
            => JsonNode.Parse(File.ReadAllText(path, Encoding.UTF8)) as JsonObject ?? new JsonObject();

        private JsonObject ReadConfigRoot()
        {
            string path = Path.Combine(_paths.RootDirectory, "config.json");
            if (!File.Exists(path))
            {
                string example = Path.Combine(_paths.RootDirectory, "config.example.json");
                if (File.Exists(example)) File.Copy(example, path);
                else File.WriteAllText(path, "{}", new UTF8Encoding(false));
            }
            return ReadConfigRoot(path);
        }

        private void WriteConfigRoot(JsonObject root)
        {
            string path = Path.Combine(_paths.RootDirectory, "config.json");
            string temporary = path + ".tmp." + Guid.NewGuid().ToString("N");
            try
            {
                File.WriteAllText(temporary, root.ToJsonString(IndentedJson), new UTF8Encoding(false));
                File.Move(temporary, path, overwrite: true);
            }
            finally
            {
                TryDelete(temporary);
            }
        }

        private static JsonObject EnsureObject(JsonObject root, params string[] path)
        {
            JsonObject current = root;
            foreach (string segment in path)
            {
                if (current[segment] is not JsonObject child)
                {
                    child = new JsonObject();
                    current[segment] = child;
                }
                current = child;
            }
            return current;
        }

        private static void SetArray(JsonObject target, string name, IEnumerable<string> values)
            => target[name] = new JsonArray(values.Select(value => (JsonNode)JsonValue.Create(value)).ToArray());

        private static void SetOptionalString(JsonObject target, string name, string value)
        {
            if (string.IsNullOrWhiteSpace(value)) target.Remove(name);
            else target[name] = value.Trim();
        }

        private static void AddPoolArgument(List<string> arguments, string option, string value)
        {
            string normalized = NormalizePoolText(value);
            if (normalized.Length > 0)
                arguments.AddRange(new[] { option, normalized });
        }

        private static void AddCountryArgument(List<string> arguments, string option, string value)
        {
            string normalized = (value ?? "").Trim().ToUpperInvariant();
            if (normalized.Length > 0)
                arguments.AddRange(new[] { option, normalized });
        }

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
