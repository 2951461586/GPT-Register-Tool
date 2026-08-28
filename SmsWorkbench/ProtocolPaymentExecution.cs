namespace SmsWorkbench
{
    public sealed record ProtocolPaymentExecutionRequest(
        string PaymentMethod,
        string TargetCountry,
        string Proxy,
        string CheckoutProxyPool,
        string ApproveProxyPool,
        bool JitRefresh,
        bool ProbeOnly,
        bool RequireZero,
        bool RequireBaToken,
        string BlikCode,
        string CheckoutCountry,
        string ApproveCountry,
        string UpdateCountry,
        string AccountEmail,
        string SessionFile);

    public sealed record ProtocolPaymentExecutionPlan(
        string TaskName,
        string StatusText,
        IReadOnlyList<string> Arguments,
        string Operation,
        bool MayHaveSideEffects);

    public static class ProtocolPaymentExecutionPlanner
    {
        private static readonly string[] ListSeparators = ["\r\n", "\n", ",", ";"];
        public static ProtocolPaymentExecutionPlan Create(ProtocolPaymentExecutionRequest request)
        {
            ArgumentNullException.ThrowIfNull(request);

            string method = PaymentMethods.Normalize(request.PaymentMethod);
            string accountEmail = (request.AccountEmail ?? "").Trim();
            string sessionFile = (request.SessionFile ?? "").Trim();
            if (accountEmail.Length == 0 && sessionFile.Length == 0)
                throw new InvalidOperationException("协议支付需要账号或 Session 文件");

            var arguments = new List<string>
            {
                "--extract-payment-link",
                "--payment-method", method,
                "--target-country", Country(request.TargetCountry, "US"),
            };

            if (accountEmail.Length > 0)
            {
                arguments.AddRange(new[] { "--email", accountEmail });
                if (sessionFile.Length > 0)
                    arguments.AddRange(new[] { "--session-file", sessionFile });
            }
            else
            {
                arguments.AddRange(new[] { "--session-file", sessionFile });
            }

            string proxy = (request.Proxy ?? "").Trim();
            if (proxy.Length > 0)
                arguments.AddRange(new[] { "--proxy", proxy });
            else
            {
                AddPoolArgument(arguments, "--checkout-proxy-pool", request.CheckoutProxyPool);
                AddPoolArgument(arguments, "--approve-proxy-pool", request.ApproveProxyPool);
            }

            if (accountEmail.Length > 0 && !request.JitRefresh)
                arguments.Add("--no-jit-at-refresh");
            if (request.ProbeOnly)
                arguments.Add("--payment-probe-only");

            AddCountryArgument(arguments, "--checkout-proxy-country", request.CheckoutCountry);
            AddCountryArgument(arguments, "--approve-proxy-country", request.ApproveCountry);
            AddCountryArgument(arguments, "--update-proxy-country", request.UpdateCountry);

            if (!request.RequireZero)
                arguments.Add("--no-require-zero");
            if (method == "paypal" && request.RequireBaToken)
                arguments.Add("--require-ba-token");
            string blikCode = (request.BlikCode ?? "").Trim();
            if (!request.ProbeOnly && method == "blik" && blikCode.Length > 0)
                arguments.AddRange(new[] { "--blik-code", blikCode });

            string methodLabel = PaymentMethods.DisplayName(method);
            bool mayHaveSideEffects = !request.ProbeOnly && method != "direct_card";
            if (request.ProbeOnly)
            {
                return new ProtocolPaymentExecutionPlan(
                    methodLabel + " 支付能力探测",
                    "正在执行 " + methodLabel + " Checkout / Stripe init 能力探测...",
                    arguments,
                    "payment_method_capability_probe",
                    false);
            }
            if (method == "blik")
            {
                return new ProtocolPaymentExecutionPlan(
                    methodLabel + " 协议支付",
                    "正在执行 " + methodLabel + " 协议支付...",
                    arguments,
                    "execute_payment",
                    true);
            }
            return new ProtocolPaymentExecutionPlan(
                methodLabel + " 协议提链",
                "正在执行 " + methodLabel + " 协议提链...",
                arguments,
                "extract_link",
                mayHaveSideEffects);
        }

        public static IReadOnlyList<string> CreateProxyTestArguments(
            string paymentMethod,
            string proxy,
            string checkoutProxyPool,
            string approveProxyPool,
            string checkoutCountry,
            string approveCountry,
            string updateCountry)
        {
            string method = PaymentMethods.Normalize(paymentMethod);
            var arguments = new List<string>
            {
                "--test-payment-proxies",
                "--payment-method",
                method,
            };
            string proxyValue = (proxy ?? "").Trim();
            if (proxyValue.Length > 0)
                arguments.AddRange(new[] { "--proxy", proxyValue });
            else
            {
                AddPoolArgument(arguments, "--checkout-proxy-pool", checkoutProxyPool);
                AddPoolArgument(arguments, "--approve-proxy-pool", approveProxyPool);
            }
            AddCountryArgument(arguments, "--checkout-proxy-country", checkoutCountry);
            AddCountryArgument(arguments, "--approve-proxy-country", approveCountry);
            AddCountryArgument(arguments, "--update-proxy-country", updateCountry);
            return arguments;
        }

        private static void AddCountryArgument(List<string> arguments, string option, string country)
        {
            string normalized = Country(country, "");
            if (normalized.Length > 0)
                arguments.AddRange(new[] { option, normalized });
        }

        private static void AddPoolArgument(List<string> arguments, string option, string value)
        {
            string normalized = string.Join(
                ProxyInputNormalizer.LineSeparator,
                (value ?? "")
                    .Split(ListSeparators, StringSplitOptions.RemoveEmptyEntries)
                    .Select(item => item.Trim())
                    .Where(item => item.Length > 0)
                    .Distinct(StringComparer.OrdinalIgnoreCase));
            if (normalized.Length > 0)
                arguments.AddRange(new[] { option, normalized });
        }

        private static string Country(string value, string fallback)
        {
            string normalized = (value ?? "").Trim().ToUpperInvariant();
            return normalized.Length > 0 ? normalized : fallback;
        }

    }

    public sealed record ProtocolPaymentResultPresentation(
        string Text,
        string Url,
        string QrPath,
        string TerminalState = "",
        bool Retryable = false,
        bool RequiresReconciliation = false,
        string Operation = "");

    public static class ProtocolPaymentResultPresenter
    {
        public static ProtocolPaymentResultPresentation Parse(string result)
        {
            string rawResult = result ?? "";
            try
            {
                using JsonDocument json = JsonDocument.Parse(rawResult);
                JsonElement root = json.RootElement;
                bool ok = root.TryGetProperty("ok", out JsonElement okElement)
                    && okElement.ValueKind == JsonValueKind.True;
                string operation = StringValue(root, "operation");
                if (!ok)
                    return Failed(root, operation);

                string terminalState = TerminalState(root);
                if (terminalState is "cancelled" or "unknown" or "timed_out")
                    return Failed(root, operation);

                var text = new StringBuilder();
                bool paymentCompleted = operation == "execute_payment"
                    && string.Equals(StringValue(root, "status"), "completed", StringComparison.OrdinalIgnoreCase);
                bool capabilityCompleted = operation == "payment_method_capability_probe";
                text.AppendLine(paymentCompleted
                    ? "[成功] 支付已完成"
                    : capabilityCompleted ? "[成功] 能力探测完成" : "[成功] 提取成功!");
                text.AppendLine();

                AppendNonEmptyString(text, root, "message", "", rejectWhitespace: true);
                if (root.TryGetProperty("probe", out JsonElement probe) && probe.ValueKind == JsonValueKind.Object)
                {
                    string probeStatus = probe.TryGetProperty("status_code", out JsonElement statusCode)
                        ? statusCode.ToString()
                        : "";
                    if (probeStatus.Length > 0)
                        text.AppendLine(CultureInfo.InvariantCulture, $"AT 探测: HTTP {probeStatus}");
                }
                if (root.TryGetProperty("refreshed", out JsonElement refreshed)
                    && refreshed.ValueKind is JsonValueKind.True or JsonValueKind.False)
                    text.AppendLine(CultureInfo.InvariantCulture, $"JIT 刷新: {(refreshed.GetBoolean() ? "已获取新 AT" : "未刷新")}");
                if (root.TryGetProperty("token_telemetry", out JsonElement telemetry)
                    && telemetry.ValueKind == JsonValueKind.Object)
                {
                    if (telemetry.TryGetProperty("age_seconds", out JsonElement age))
                        text.AppendLine(CultureInfo.InvariantCulture, $"AT 年龄: {age} 秒");
                    if (telemetry.TryGetProperty("expires_in_seconds", out JsonElement expiresIn))
                        text.AppendLine(CultureInfo.InvariantCulture, $"AT 剩余: {expiresIn} 秒");
                }

                string url = "";
                if (root.TryGetProperty("upi_uri", out JsonElement upiUri)
                    && !string.IsNullOrEmpty(upiUri.GetString()))
                {
                    url = upiUri.GetString() ?? "";
                    text.AppendLine(CultureInfo.InvariantCulture, $"UPI URI: {SensitiveDataSanitizer.Redact(url)}");
                }
                else if (root.TryGetProperty("url", out JsonElement urlElement)
                    && !string.IsNullOrEmpty(urlElement.GetString()))
                {
                    url = urlElement.GetString() ?? "";
                    text.AppendLine(CultureInfo.InvariantCulture, $"链接: {SensitiveDataSanitizer.Redact(url)}");
                }

                AppendString(text, root, "hosted_url", "托管 URL: ");
                AppendString(text, root, "link_type", "链接类型: ");
                AppendString(text, root, "run_id", "任务 ID: ");
                AppendString(text, root, "manager_state", "状态机: ");
                AppendString(text, root, "state", "执行状态: ");
                AppendString(text, root, "operation", "执行动作: ");
                AppendString(text, root, "subscription_plan", "订阅状态: ");
                AppendString(text, root, "payment_method", "支付方式: ");

                if (root.TryGetProperty("card_last4", out JsonElement last4)
                    && !string.IsNullOrWhiteSpace(last4.GetString()))
                    text.AppendLine("卡片: [REDACTED]");

                string qrPath = root.TryGetProperty("qr_path", out JsonElement qrPathElement)
                    ? qrPathElement.GetString() ?? ""
                    : "";
                if (qrPath.Length > 0)
                    text.AppendLine(CultureInfo.InvariantCulture, $"QR 图片: {qrPath}");

                AppendString(text, root, "cs_id", "CS ID: ");
                if (root.TryGetProperty("amount", out JsonElement amount))
                    text.AppendLine(CultureInfo.InvariantCulture, $"金额: {amount}");
                AppendString(text, root, "currency", "货币: ");
                AppendNonEmptyString(text, root, "coupon_name", "优惠券: ", rejectWhitespace: false);
                if (root.TryGetProperty("approval_ok", out JsonElement approval))
                    text.AppendLine(CultureInfo.InvariantCulture, $"审批状态: {(approval.GetBoolean() ? "已批准" : "待处理/失败")}");
                if (root.TryGetProperty("expires_at", out JsonElement expiresAt))
                {
                    try
                    {
                        long expires = expiresAt.GetInt64();
                        if (expires > 0)
                        {
                            DateTime local = DateTimeOffset.FromUnixTimeSeconds(expires).LocalDateTime;
                            text.AppendLine(CultureInfo.InvariantCulture, $"过期时间: {local:yyyy-MM-dd HH:mm:ss}");
                        }
                    }
                    catch
                    {
                    }
                }
                AppendString(text, root, "target_country", "国家: ");
                AppendString(text, root, "warning", "警告: ");

                return new ProtocolPaymentResultPresentation(
                    text.ToString().TrimEnd(),
                    url,
                    qrPath,
                    "completed",
                    false,
                    false,
                    operation);
            }
            catch
            {
                return new ProtocolPaymentResultPresentation(SensitiveDataSanitizer.Redact(rawResult), "", "");
            }
        }

        public static ProtocolPaymentResultPresentation Aborted(
            ProtocolPaymentExecutionPlan plan,
            string requestedState)
        {
            string requested = CanonicalState(requestedState);
            if (requested.Length == 0)
                requested = "failed";
            bool requiresReconciliation = plan?.MayHaveSideEffects == true;
            string terminalState = requiresReconciliation ? "unknown" : requested;
            bool retryable = terminalState == "timed_out";
            string operation = plan?.Operation ?? "";
            string text = terminalState switch
            {
                "unknown" => "[结果未知，请先核对账号状态，不要重试]",
                "cancelled" => "[已取消] 协议支付任务已终止",
                "timed_out" => "[已超时] 协议支付任务超时，可按策略重试",
                _ => "[失败] 协议支付任务未完成"
            };
            if (operation.Length > 0)
                text += $"\n执行动作: {operation}";
            if (requiresReconciliation)
                text += "\n需要对账：请求可能已到达支付服务。";
            return new ProtocolPaymentResultPresentation(
                text,
                "",
                "",
                terminalState,
                retryable,
                requiresReconciliation,
                operation);
        }

        private static ProtocolPaymentResultPresentation Failed(JsonElement root, string operation)
        {
            string error = SensitiveDataSanitizer.Redact(StringValue(root, "error"));
            string decisionText = SensitiveDataSanitizer.Redact(StringValue(root, "decision_text"));
            string message = SensitiveDataSanitizer.Redact(StringValue(root, "message"));
            string decision = StringValue(root, "decision");
            string errorCode = StringValue(root, "error_code");
            string state = TerminalState(root);
            if (state.Length == 0)
                state = "failed";
            bool requiresReconciliation = state == "unknown"
                || BoolValue(root, "requires_reconciliation")
                || BoolValue(root, "outcome_unknown");
            bool retryable = BoolValue(root, "retryable")
                && !requiresReconciliation
                && state != "cancelled";
            string prefix = state switch
            {
                "unknown" => "[结果未知，请先核对账号状态，不要重试]",
                "cancelled" => "[已取消]",
                "timed_out" => "[已超时]",
                _ => "[失败]"
            };
            string summary = FirstNonEmpty(decisionText, error, message, decision, "协议支付未完成");
            var text = new StringBuilder($"{prefix} {summary}".TrimEnd());
            if (decision.Length > 0 && !string.Equals(decision, summary, StringComparison.Ordinal))
                text.AppendLine().Append("判定: ").Append(SensitiveDataSanitizer.Redact(decision));
            if (errorCode.Length > 0)
                text.AppendLine().Append("错误代码: ").Append(SensitiveDataSanitizer.Redact(errorCode));
            string errorStage = StringValue(root, "error_stage");
            if (errorStage.Length > 0)
                text.AppendLine().Append("错误阶段: ").Append(SensitiveDataSanitizer.Redact(errorStage));
            string paymentMethod = StringValue(root, "payment_method");
            if (paymentMethod.Length > 0)
                text.AppendLine().Append("支付方式: ").Append(SensitiveDataSanitizer.Redact(paymentMethod));
            string subscriptionPlan = StringValue(root, "subscription_plan");
            if (subscriptionPlan.Length > 0)
                text.AppendLine().Append("订阅状态: ").Append(SensitiveDataSanitizer.Redact(subscriptionPlan));
            if (root.TryGetProperty("amount_due", out JsonElement amountDue)
                && amountDue.ValueKind is JsonValueKind.Number or JsonValueKind.String)
            {
                text.AppendLine().Append("应付金额: ").Append(amountDue.ToString());
                string currency = StringValue(root, "currency");
                if (currency.Length > 0)
                    text.Append(' ').Append(SensitiveDataSanitizer.Redact(currency.ToUpperInvariant()));
            }
            if (requiresReconciliation)
                text.AppendLine().Append("需要对账：请求可能已到达支付服务。");
            else if (retryable)
                text.AppendLine().Append("可重试: 是");
            return new ProtocolPaymentResultPresentation(
                text.ToString(),
                "",
                "",
                state,
                retryable,
                requiresReconciliation,
                operation);
        }

        private static string TerminalState(JsonElement root)
        {
            if (BoolValue(root, "requires_reconciliation") || BoolValue(root, "outcome_unknown"))
                return "unknown";
            foreach (string property in new[] { "terminal_state", "status", "state", "outcome", "manager_state" })
            {
                string state = CanonicalState(StringValue(root, property));
                if (state.Length > 0)
                    return state;
            }
            return "";
        }

        private static string CanonicalState(string value)
        {
            string normalized = (value ?? "").Trim().ToLowerInvariant()
                .Replace('-', '_')
                .Replace(' ', '_');
            if (normalized is "cancelled" or "canceled" or "cancelled_by_user" or "canceled_by_user"
                or "interrupted" or "keyboard_interrupt")
                return "cancelled";
            if (normalized is "timed_out" or "timeout" or "timeout_expired" or "extractor_timeout")
                return "timed_out";
            if (normalized is "unknown" or "outcome_unknown" or "payment_outcome_unknown"
                or "indeterminate" or "inconclusive")
                return "unknown";
            return "";
        }

        private static string StringValue(JsonElement root, string propertyName)
        {
            if (!root.TryGetProperty(propertyName, out JsonElement value))
                return "";
            return value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : value.ToString();
        }

        private static string FirstNonEmpty(params string[] values)
        {
            return values.FirstOrDefault(value => !string.IsNullOrWhiteSpace(value))?.Trim() ?? "";
        }

        private static bool BoolValue(JsonElement root, string propertyName)
        {
            return root.TryGetProperty(propertyName, out JsonElement value)
                && value.ValueKind == JsonValueKind.True;
        }

        private static void AppendString(StringBuilder text, JsonElement root, string propertyName, string prefix)
        {
            if (!root.TryGetProperty(propertyName, out JsonElement value))
                return;
            text.AppendLine(prefix + SensitiveDataSanitizer.Redact(value.GetString()));
        }

        private static void AppendNonEmptyString(
            StringBuilder text,
            JsonElement root,
            string propertyName,
            string prefix,
            bool rejectWhitespace)
        {
            if (!root.TryGetProperty(propertyName, out JsonElement value))
                return;
            string content = value.GetString() ?? "";
            bool empty = rejectWhitespace ? string.IsNullOrWhiteSpace(content) : content.Length == 0;
            if (!empty)
                text.AppendLine(prefix + SensitiveDataSanitizer.Redact(content));
        }
    }
}
