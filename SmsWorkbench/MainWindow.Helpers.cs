namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Path/config helpers, status formatting, external open/copy/log helpers
        private void AddNonPaymentProxy(List<string> args)
        {
            args.Add("--proxy");
            args.Add(LocalNonPaymentProxy);
        }

        private string GetConfiguredCfWorkerDomain()
        {
            try
            {
                var email = GetSection(ReadJsonObject(Path.Combine(rootDir, "config.json")), "email_registration");
                string domain = GetString(email, "cfworker_domain").Trim().TrimStart('@');
                if (domain.Length > 0) return domain;
                if (email.TryGetValue("cfworker", out object nestedRaw) && nestedRaw is Dictionary<string, object> nested)
                {
                    domain = GetString(nested, "domain").Trim().TrimStart('@');
                    if (domain.Length > 0) return domain;
                }
            }
            catch
            {
            }
            return "liziai.cloud";
        }

        private void AddPaypalOption(List<string> args, string paymentMethod = "paypal", bool skipPaymentLink = false)
        {
            if (skipPaymentLink || SkipPaypalLink)
            {
                args.Add("--skip-paypal-link");
                return;
            }
            args.Add("--payment-method");
            args.Add(NormalizePaymentMethod(paymentMethod));
        }

        private string NormalizePaymentMethod(string paymentMethod)
        {
            string value = (paymentMethod ?? "").Trim().ToLowerInvariant().Replace("-", "_").Replace(" ", "_");
            return value switch
            {
                "gopay" or "go_pay" => "gopay",
                "upi" or "upiqr" or "upi_qr" => "upi",
                "ideal" => "ideal",
                "pix" => "pix",
                "kakao" or "kakao_pay" => "kakao",
                "blik" => "blik",
                "twint" => "twint",
                _ => "paypal"
            };
        }

        private void AddPaymentMethodItems(ComboBox box)
        {
            box.Items.Add(new ComboBoxItem { Content = "PayPal 支付链接", Tag = "paypal" });
            box.Items.Add(new ComboBoxItem { Content = "GoPay 印尼协议", Tag = "gopay" });
            box.Items.Add(new ComboBoxItem { Content = "UPI 印度协议", Tag = "upi" });
            box.Items.Add(new ComboBoxItem { Content = "iDEAL 荷兰协议", Tag = "ideal" });
            box.Items.Add(new ComboBoxItem { Content = "PIX 巴西协议", Tag = "pix" });
            box.Items.Add(new ComboBoxItem { Content = "Kakao Pay 韩国协议", Tag = "kakao" });
            box.Items.Add(new ComboBoxItem { Content = "BLIK 波兰协议", Tag = "blik" });
            box.Items.Add(new ComboBoxItem { Content = "TWINT 瑞士协议", Tag = "twint" });
        }

        private int CountValue()
        {
            return int.TryParse(CountText, out int value) && value > 0 ? value : 1;
        }

        private int PageSizeValue()
        {
            return int.TryParse(PageSizeText, out int value) && value > 0 ? Math.Min(value, 500) : 25;
        }

        private string GetSessionsDir()
        {
            return Path.Combine(rootDir, "sessions");
        }

        private string GetDatabasePath()
        {
            string configured = ConfigString("storage", "sqlite_path");
            if (configured.Length == 0) return Path.Combine(rootDir, "runtime", "accounts.sqlite3");
            string expanded = Environment.ExpandEnvironmentVariables(configured);
            return Path.IsPathRooted(expanded) ? expanded : Path.Combine(rootDir, expanded);
        }

        private string GetMailboxTokenFile()
        {
            string configured = ConfigString("email_registration", "token_file");
            string expanded = configured.Length > 0 ? Environment.ExpandEnvironmentVariables(configured) : "mailbox_tokens.txt";
            return Path.IsPathRooted(expanded) ? expanded : Path.Combine(rootDir, expanded);
        }

        private string ConfigString(string section, string key)
        {
            string path = Path.Combine(rootDir, "config.json");
            if (!File.Exists(path)) return "";
            try
            {
                Dictionary<string, object> data = ReadJsonObject(path);
                if (!data.TryGetValue(section, out object sectionObj)) return "";
                if (sectionObj is not Dictionary<string, object> sectionData) return "";
                return sectionData.TryGetValue(key, out object value) ? Convert.ToString(value) ?? "" : "";
            }
            catch
            {
                return "";
            }
        }

        private string GetPaypalStatus(Dictionary<string, object> data)
        {
            if (!TryGetMap(data, "paypal", out Dictionary<string, object> paypal) || paypal.Count == 0)
            {
                return "已保存";
            }
            string method = GetString(data, "payment_method");
            if (method.Length == 0) method = GetString(paypal, "payment_method");
            if (method.Length == 0) method = GetString(paypal, "method");
            string prefix = NormalizePaymentMethod(method) == "paypal" ? "" : PaymentMethodLabel(method) + " ";
            if (IsPaymentLinkMethodMismatch(data, method)) return prefix + "支付失败";
            string status = GetString(data, "paypal_status");
            if (status.Length == 0) status = GetString(paypal, "status");
            if (status.Equals("completed", StringComparison.OrdinalIgnoreCase)) return prefix + "支付完成✅";
            if (status.Equals("pm_created", StringComparison.OrdinalIgnoreCase)) return prefix + "PM已创建✅";
            if (status.Equals("otp_required", StringComparison.OrdinalIgnoreCase)) return prefix + "待输入OTP";
            if (status.Equals("manual_confirmation_required", StringComparison.OrdinalIgnoreCase)) return PaymentPendingStatus(method);
            if (status.Equals("link_ready", StringComparison.OrdinalIgnoreCase)) return PaymentPendingStatus(method);
            string ok = GetString(paypal, "ok").ToLowerInvariant();
            if (ok == "true") return PaymentPendingStatus(method);
            string error = GetString(paypal, "error");
            return error.Length > 0 ? prefix + "失败" : "已保存";
        }

        private string GetPaypalUrl(Dictionary<string, object> data)
        {
            if (!TryGetMap(data, "paypal", out Dictionary<string, object> paypal)) return "";
            return GetString(paypal, "url");
        }

        private bool IsCpaImported(string rawJson)
        {
            if (string.IsNullOrWhiteSpace(rawJson)) return false;
            try
            {
                return IsCpaImported(JsonTextToObject(rawJson));
            }
            catch
            {
                return false;
            }
        }

        private bool IsCpaImported(Dictionary<string, object> data)
        {
            if (!TryGetMap(data, "cpa_import", out Dictionary<string, object> cpaImport)) return false;
            return GetString(cpaImport, "ok").Equals("true", StringComparison.OrdinalIgnoreCase);
        }

        private string GetImportedStatus(string rawJson)
        {
            if (string.IsNullOrWhiteSpace(rawJson)) return "";
            try
            {
                return GetImportedStatus(JsonTextToObject(rawJson));
            }
            catch
            {
                return "";
            }
        }

        private string GetImportedStatus(Dictionary<string, object> data)
        {
            bool cpaImported = IsImportOk(data, "cpa_import");
            bool sub2Imported = IsImportOk(data, "sub2api_import");
            if (cpaImported && sub2Imported) return "已导入CPA/SUB2";
            if (cpaImported) return "已导入CPA";
            if (sub2Imported) return "已导入SUB2";
            return "";
        }

        private bool IsImportOk(Dictionary<string, object> data, string key)
        {
            if (!TryGetMap(data, key, out Dictionary<string, object> importData)) return false;
            return GetString(importData, "ok").Equals("true", StringComparison.OrdinalIgnoreCase);
        }

        private string GetPaypalAmount(string rawJson)
        {
            if (string.IsNullOrWhiteSpace(rawJson)) return "";
            try
            {
                return GetPaypalAmount(JsonTextToObject(rawJson));
            }
            catch
            {
                return "";
            }
        }

        private string GetVerifiedPhone(string rawJson)
        {
            if (string.IsNullOrWhiteSpace(rawJson)) return "";
            try
            {
                return GetVerifiedPhone(JsonTextToObject(rawJson));
            }
            catch
            {
                return "";
            }
        }

        private string GetVerifiedPhone(Dictionary<string, object> data)
        {
            string topLevelPhone = NormalizePhoneText(FirstNonEmpty(GetString(data, "phone"), GetString(data, "phone_number")));
            if (TryGetMap(data, "response", out Dictionary<string, object> response)
                && TryGetMap(response, "phone_verification", out Dictionary<string, object> phoneVerification))
            {
                bool ok = GetString(phoneVerification, "ok").Equals("true", StringComparison.OrdinalIgnoreCase)
                    || GetString(phoneVerification, "ok").Equals("1", StringComparison.OrdinalIgnoreCase);
                string phone = NormalizePhoneText(FirstNonEmpty(
                    GetString(phoneVerification, "phone"),
                    GetString(phoneVerification, "phone_number"),
                    topLevelPhone
                ));
                return ok ? phone : "";
            }

            string refreshTokenStatus = GetString(data, "refresh_token_status");
            bool hasRt = refreshTokenStatus.Equals("oauth_present", StringComparison.OrdinalIgnoreCase)
                || refreshTokenStatus.Equals("legacy_present", StringComparison.OrdinalIgnoreCase);
            return hasRt ? topLevelPhone : "";
        }

        private string NormalizePhoneText(string raw)
        {
            string value = (raw ?? "").Trim();
            if (value.Length == 0) return "";
            string digits = new string(value.Where(char.IsDigit).ToArray());
            if (digits.Length == 0) return "";
            return "+" + digits;
        }

        private string GetPaypalAmount(Dictionary<string, object> data)
        {
            if (!TryGetMap(data, "paypal", out Dictionary<string, object> paypal)) return "";
            string currency = GetString(paypal, "currency").Trim().ToUpperInvariant();
            string rawAmount = FirstNonEmpty(
                GetString(paypal, "amount_due"),
                GetString(paypal, "due"),
                GetString(paypal, "expected_amount")
            );
            if (rawAmount.Length == 0) return "";
            if (!decimal.TryParse(rawAmount, out decimal amount)) return currency.Length > 0 ? rawAmount + " " + currency : rawAmount;
            decimal displayAmount = amount / 100m;
            string text = displayAmount.ToString("0.00");
            return currency.Length > 0 ? text + " " + currency : text;
        }

        private string GetAccountPlanType(Dictionary<string, object> data)
        {
            if (data == null) return "free";
            string k12Status = FirstNonEmpty(
                GetString(data, "k12_status"),
                NestedString(data, "k12", "status"),
                NestedString(data, "workspace_scan", "account_type_after"),
                NestedString(data, "account_scan", "workspace", "account_type_after")
            ).Trim().ToLowerInvariant();
            if ((k12Status.Contains("k12") || GetString(data, "k12_workspace_id").Length > 0 || NestedString(data, "k12", "workspace_id").Length > 0)
                && !k12Status.Contains("left")
                && !k12Status.Contains("fallback_free"))
            {
                return "k12";
            }

            string value = FirstNonEmpty(
                GetString(data, "subscription_type"),
                GetString(data, "plan_type"),
                GetString(data, "planType"),
                NestedString(data, "account", "plan_type"),
                NestedString(data, "account", "planType"),
                NestedString(data, "auth_session", "account", "plan_type"),
                NestedString(data, "auth_session", "account", "planType"),
                JwtAuthString(GetString(data, "access_token"), "chatgpt_plan_type"),
                JwtAuthString(GetString(data, "access_token"), "plan_type"),
                GetString(data, "account_type")
            ).Trim().ToLowerInvariant();

            if (value.Contains("pro")) return "pro";
            if (value.Contains("team") || value.Contains("business") || value.Contains("enterprise")) return "team";
            if (value.Contains("k12") || value.Contains("edu")) return "k12";
            if (value.Contains("plus")) return "plus";
            return "free";
        }

        private string GetQuotaStatus(Dictionary<string, object> data)
        {
            if (data == null) return "";

            // Try wham_usage from quota.last_result.wham_usage (stored by refresh_local_quota_statuses)
            string whamLabel = FormatWhamUsageLabel(ExtractWhamUsage(data));
            if (whamLabel.Length > 0) return whamLabel;

            string explicitValue = FirstNonEmpty(
                GetString(data, "quota_status"),
                GetString(data, "quota"),
                GetString(data, "usage_status"),
                NestedString(data, "quota", "status"),
                NestedString(data, "quota", "message"),
                NestedString(data, "usage", "status"),
                NestedString(data, "usage", "message"),
                NestedString(data, "account", "quota_status"),
                NestedString(data, "auth_session", "account", "quota_status")
            ).Trim();
            if (explicitValue.Length > 0) return explicitValue;
            string remaining = FirstNonEmpty(NestedString(data, "quota", "remaining"), NestedString(data, "usage", "remaining"));
            string limit = FirstNonEmpty(NestedString(data, "quota", "limit"), NestedString(data, "usage", "limit"));
            if (remaining.Length > 0 || limit.Length > 0) return remaining + (limit.Length > 0 ? "/" + limit : "");
            if (GetString(data, "access_token").Trim().Length > 0) return "待刷新";
            return "未知";
        }

        /// <summary>
        /// Extract wham_usage 5h/7d structured data from session JSON.
        /// Looks under quota.last_result.wham_usage (stored by probe_local_codex_quota -> mark_quota_status).
        /// </summary>
        private Dictionary<string, object> ExtractWhamUsage(Dictionary<string, object> data)
        {
            if (data == null) return null;

            // Path 1: data["quota"]["last_result"]["wham_usage"]
            object quotaObj = null;
            if (data.TryGetValue("quota", out quotaObj) && quotaObj is Dictionary<string, object> quota)
            {
                if (quota.TryGetValue("last_result", out object lr) && lr is Dictionary<string, object> lastResult)
                {
                    if (lastResult.TryGetValue("wham_usage", out object wham) && wham is Dictionary<string, object> whamDict)
                        return whamDict;
                }
            }

            // Path 2: data["wham_usage"] (direct)
            if (data.TryGetValue("wham_usage", out object direct) && direct is Dictionary<string, object> directDict)
                return directDict;

            // Path 3: data["quota"]["wham_usage"]
            if (quotaObj is Dictionary<string, object> quota2 && quota2.TryGetValue("wham_usage", out object wham2) && wham2 is Dictionary<string, object> whamDict2)
                return whamDict2;

            return null;
        }

        /// <summary>
        /// Format wham_usage into display string: "5h: 3K/10K (30%) | 7d: 12K/50K (24%)"
        /// </summary>
        private string FormatWhamUsageLabel(Dictionary<string, object> wham)
        {
            if (wham == null || wham.Count == 0) return "";
            var parts = new List<string>();
            foreach (string windowKey in new[] { "5h", "7d" })
            {
                if (wham.TryGetValue(windowKey, out object w) && w is Dictionary<string, object> window)
                {
                    long used = GetLongValue(window, "used");
                    long limit = GetLongValue(window, "limit");
                    double percent = GetDoubleValue(window, "percent");
                    if (used > 0 || limit > 0)
                        parts.Add($"{windowKey}: {FmtTokenCount(used)}/{FmtTokenCount(limit)} ({percent:F0}%)");
                }
            }
            return parts.Count > 0 ? string.Join(" | ", parts) : "";
        }

        private string FmtTokenCount(long n)
        {
            if (n >= 1_000_000) return $"{n / 1_000_000.0:F1}M";
            if (n >= 1_000) return $"{n / 1_000.0:F1}K";
            return n.ToString();
        }

        private long GetLongValue(Dictionary<string, object> data, string key)
        {
            if (data == null || !data.TryGetValue(key, out object val) || val == null) return 0;
            if (val is long l) return l;
            if (val is int i) return i;
            if (val is double d) return (long)d;
            if (long.TryParse(val.ToString(), out long parsed)) return parsed;
            return 0;
        }

        private double GetDoubleValue(Dictionary<string, object> data, string key)
        {
            if (data == null || !data.TryGetValue(key, out object val) || val == null) return 0;
            if (val is double d) return d;
            if (val is long l) return l;
            if (val is int i) return i;
            if (double.TryParse(val.ToString(), out double parsed)) return parsed;
            return 0;
        }

        /// <summary>
        /// Populate PoolRow quota fields from wham_usage data in session JSON.
        /// </summary>
        private void PopulateQuotaFields(PoolRow row, Dictionary<string, object> data)
        {
            var wham = ExtractWhamUsage(data);
            if (wham == null) return;
            foreach (string windowKey in new[] { "5h", "7d" })
            {
                if (!wham.TryGetValue(windowKey, out object w) || !(w is Dictionary<string, object> window)) continue;
                long used = GetLongValue(window, "used");
                long limit = GetLongValue(window, "limit");
                long remaining = GetLongValue(window, "remaining");
                double percent = GetDoubleValue(window, "percent");
                string usedStr = FmtTokenCount(used);
                string limitStr = FmtTokenCount(limit);
                string remStr = FmtTokenCount(remaining);
                string pctStr = percent.ToString("F0") + "%";
                if (windowKey == "5h")
                {
                    row.Quota5hUsed = usedStr;
                    row.Quota5hLimit = limitStr;
                    row.Quota5hRemaining = remStr;
                    row.Quota5hPercent = pctStr;
                }
                else
                {
                    row.Quota7dUsed = usedStr;
                    row.Quota7dLimit = limitStr;
                    row.Quota7dRemaining = remStr;
                    row.Quota7dPercent = pctStr;
                }
            }
        }

        private string NestedString(Dictionary<string, object> data, params string[] path)
        {
            object current = data;
            foreach (string key in path)
            {
                if (current is not Dictionary<string, object> map) return "";
                if (!map.TryGetValue(key, out current)) return "";
            }
            return Convert.ToString(current) ?? "";
        }

        private string JwtAuthString(string token, string key)
        {
            try
            {
                string[] parts = (token ?? "").Split('.');
                if (parts.Length < 2 || parts[1].Length == 0) return "";
                string payload = parts[1].Replace('-', '+').Replace('_', '/');
                payload = payload.PadRight(payload.Length + ((4 - payload.Length % 4) % 4), '=');
                string json = Encoding.UTF8.GetString(Convert.FromBase64String(payload));
                var obj = JsonTextToObject(json);
                if (TryGetMap(obj, "https://api.openai.com/auth", out Dictionary<string, object> auth))
                {
                    return GetString(auth, key);
                }
                return GetString(obj, key);
            }
            catch
            {
                return "";
            }
        }

        private bool IsPaymentLinkMethodMismatch(string rawJson, string paymentMethod)
        {
            if (string.IsNullOrWhiteSpace(rawJson)) return false;
            try
            {
                return IsPaymentLinkMethodMismatch(JsonTextToObject(rawJson), paymentMethod);
            }
            catch
            {
                return false;
            }
        }

        private bool IsPaymentLinkMethodMismatch(Dictionary<string, object> data, string paymentMethod)
        {
            string requested = NormalizePaymentMethod(paymentMethod);
            if (!TryGetMap(data, "paypal", out Dictionary<string, object> paypal) || paypal.Count == 0) return false;
            string savedMethod = NormalizePaymentMethod(FirstNonEmpty(
                GetString(paypal, "payment_method"),
                GetString(paypal, "method"),
                GetString(paypal, "type")
            ));
            bool hasSavedMethod = GetString(paypal, "payment_method").Length > 0
                || GetString(paypal, "method").Length > 0
                || GetString(paypal, "type").Length > 0;
            string currency = GetString(paypal, "currency").Trim().ToLowerInvariant();
            string detected = hasSavedMethod ? savedMethod : "";
            if (detected.Length == 0)
            {
                foreach (string candidate in new[] { "paypal", "gopay", "upi", "ideal", "pix", "kakao", "blik", "twint" })
                {
                    if (PaymentMethodTypesContain(paypal, candidate))
                    {
                        detected = candidate;
                        break;
                    }
                }
            }
            if (detected.Length == 0)
            {
                detected = currency switch
                {
                    "idr" => "gopay",
                    "inr" => "upi",
                    "brl" => "pix",
                    "krw" => "kakao",
                    "pln" => "blik",
                    "chf" => "twint",
                    "eur" when requested == "ideal" => "ideal",
                    "usd" => "paypal",
                    _ => ""
                };
            }
            return detected.Length > 0 && detected != requested;
        }

        private bool PaymentMethodTypesContain(Dictionary<string, object> paypal, string expected)
        {
            if (!paypal.TryGetValue("payment_method_types", out object raw) || raw == null) return false;
            string target = expected.Trim().ToLowerInvariant();
            if (raw is List<object> items)
            {
                return items.Any(item => string.Equals(Convert.ToString(item)?.Trim(), target, StringComparison.OrdinalIgnoreCase));
            }
            return Convert.ToString(raw)?.IndexOf(target, StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private string FirstNonEmpty(params string[] values)
        {
            foreach (string value in values)
            {
                if (!string.IsNullOrWhiteSpace(value)) return value.Trim();
            }
            return "";
        }

        private string GetTimingText(Dictionary<string, object> data)
        {
            if (TryGetMap(data, "pipeline_timing", out Dictionary<string, object> pipeline))
            {
                string total = GetString(pipeline, "total_seconds");
                if (total.Length > 0) return total + "s";
            }
            if (TryGetMap(data, "timing", out Dictionary<string, object> timing))
            {
                string total = GetString(timing, "total_seconds");
                if (total.Length > 0) return total + "s";
            }
            if (TryGetMap(data, "paypal", out Dictionary<string, object> paypal))
            {
                return GetString(paypal, "proxy");
            }
            return "";
        }

        private string DisplayAccountStatus(string status, string paypalOk, string access, string error, string paypalStatus, string refreshTokenStatus, string importedStatus)
        {
            if (!string.IsNullOrWhiteSpace(importedStatus)) return importedStatus;
            bool hasRt = refreshTokenStatus.Equals("oauth_present", StringComparison.OrdinalIgnoreCase)
                || refreshTokenStatus.Equals("legacy_present", StringComparison.OrdinalIgnoreCase);
            if (status.Equals("account_deactivated", StringComparison.OrdinalIgnoreCase)
                || LooksAccountDeactivatedError(error)) return "账号掉号";
            if (hasRt && LooksPhoneVerificationError(error)) return "手机验证";
            if (status.Equals("at_invalid", StringComparison.OrdinalIgnoreCase)
                || status.Equals("access_token_invalid", StringComparison.OrdinalIgnoreCase)
                || status.Equals("token_invalidated", StringComparison.OrdinalIgnoreCase)
                || LooksAtInvalidError(error)) return "AT失效";
            if (status.Equals("k12_left", StringComparison.OrdinalIgnoreCase)) return "K12已退出";
            if (status.Equals("k12_joined", StringComparison.OrdinalIgnoreCase)) return "K12已进入✅";
            if (status.Equals("k12_requested", StringComparison.OrdinalIgnoreCase)) return "K12已申请";
            if (status.Equals("k12_verify_failed", StringComparison.OrdinalIgnoreCase)) return "K12未切换";
            if (paypalStatus.Equals("completed", StringComparison.OrdinalIgnoreCase)) return "支付完成✅";
            if (paypalStatus.Equals("pm_created", StringComparison.OrdinalIgnoreCase)
                || status.Equals("paypal_pm_created", StringComparison.OrdinalIgnoreCase)) return "PM已创建✅";
            if (status.Equals("paypal_failed", StringComparison.OrdinalIgnoreCase) || paypalStatus.Equals("failed", StringComparison.OrdinalIgnoreCase)) return "支付链接失败";
            if (paypalStatus.Equals("manual_confirmation_required", StringComparison.OrdinalIgnoreCase)
                || paypalStatus.Equals("link_ready", StringComparison.OrdinalIgnoreCase)
                || paypalOk == "1"
                || status.Equals("paypal_ready", StringComparison.OrdinalIgnoreCase)) return "待支付";
            if (hasRt && access.Length > 0) return "已注册";
            if (!string.IsNullOrWhiteSpace(error) || status.Equals("failed", StringComparison.OrdinalIgnoreCase)) return "失败";
            return access.Length > 0 ? "已注册" : "待处理";
        }

        private bool LooksAtInvalidError(string error)
        {
            string text = (error ?? "").ToLowerInvariant();
            return text.Contains("token_invalidated")
                || text.Contains("token_expired")
                || text.Contains("authentication token has been invalidated")
                || text.Contains("could not validate your token")
                || LooksPhoneVerificationError(text)
                || LooksAccountDeactivatedError(text)
                || text.Contains("oauth_refresh_http_401");
        }

        private bool LooksPhoneVerificationError(string error)
        {
            string text = (error ?? "").ToLowerInvariant();
            return text.Contains("secondary_phone_verification_required")
                || text.Contains("add_phone_required");
        }

        private bool LooksAccountDeactivatedError(string error)
        {
            string text = (error ?? "").ToLowerInvariant();
            return text.Contains("account_deactivated")
                || text.Contains("account_deatived")
                || text.Contains("deleted or deactivated")
                || text.Contains("account has been deleted")
                || text.Contains("account has been deactivated");
        }

        private string DisplayPayPalStatus(string paypalStatus, string paypalOk, string paypalUrl, string paymentMethod = "")
        {
            string prefix = NormalizePaymentMethod(paymentMethod) == "paypal" ? "" : PaymentMethodLabel(paymentMethod) + " ";
            if (paypalStatus.Equals("completed", StringComparison.OrdinalIgnoreCase)) return prefix + "支付完成✅";
            if (paypalStatus.Equals("pm_created", StringComparison.OrdinalIgnoreCase)) return prefix + "PM已创建✅";
            if (paypalStatus.Equals("failed", StringComparison.OrdinalIgnoreCase)) return prefix + "支付失败";
            if (paypalStatus.Equals("otp_required", StringComparison.OrdinalIgnoreCase)) return prefix + "待输入OTP";
            if (paypalStatus.Equals("manual_confirmation_required", StringComparison.OrdinalIgnoreCase)) return PaymentPendingStatus(paymentMethod);
            if (paypalStatus.Equals("link_ready", StringComparison.OrdinalIgnoreCase)) return PaymentPendingStatus(paymentMethod);
            if (paypalOk == "1" && !string.IsNullOrWhiteSpace(paypalUrl)) return PaymentPendingStatus(paymentMethod);
            if (!string.IsNullOrWhiteSpace(paypalUrl)) return PaymentPendingStatus(paymentMethod);
            return "";
        }

        private string PaymentPendingStatus(string paymentMethod)
        {
            return PaymentMethodLabel(paymentMethod) + "待支付";
        }

        private string PaymentMethodLabel(string paymentMethod)
        {
            return NormalizePaymentMethod(paymentMethod) switch
            {
                "gopay" => "GoPay",
                "upi" => "UPI",
                "ideal" => "iDEAL",
                "pix" => "PIX",
                "kakao" => "Kakao Pay",
                "blik" => "BLIK",
                "twint" => "TWINT",
                _ => "PayPal"
            };
        }

        private string DisplayRtStatus(string refreshTokenStatus)
        {
            string value = (refreshTokenStatus ?? "").Trim();
            return value.Equals("oauth_present", StringComparison.OrdinalIgnoreCase)
                || value.Equals("legacy_present", StringComparison.OrdinalIgnoreCase)
                ? "已获取"
                : "未获取";
        }

        private string DisplayRefreshTokenStatus(string refreshTokenStatus)
        {
            if (refreshTokenStatus.Equals("oauth_present", StringComparison.OrdinalIgnoreCase)) return "已获取";
            if (refreshTokenStatus.Equals("legacy_present", StringComparison.OrdinalIgnoreCase)) return "旧token";
            if (refreshTokenStatus.Equals("no_rt", StringComparison.OrdinalIgnoreCase)) return "无RT";
            if (refreshTokenStatus.Equals("missing", StringComparison.OrdinalIgnoreCase)) return "缺失";
            return refreshTokenStatus ?? "";
        }

        private string DbTimingText(Dictionary<string, string> data)
        {
            string pipeline = data.TryGetValue("pipeline_total_seconds", out string pipelineSeconds) ? pipelineSeconds : "";
            if (!string.IsNullOrWhiteSpace(pipeline) && pipeline != "0.0" && pipeline != "0") return pipeline + "s";
            string timing = data.TryGetValue("timing_total_seconds", out string timingSeconds) ? timingSeconds : "";
            return string.IsNullOrWhiteSpace(timing) || timing == "0.0" || timing == "0" ? "" : timing + "s";
        }

        private string UnixTimeText(string raw)
        {
            if (!long.TryParse(raw, out long seconds) || seconds <= 0) return "";
            return DateTimeOffset.FromUnixTimeSeconds(seconds).LocalDateTime.ToString("yyyy-MM-dd HH:mm:ss");
        }

        private string OnlyDigits(string raw)
        {
            string digits = new string((raw ?? "").Where(char.IsDigit).ToArray());
            return digits.Length == 0 ? "0" : digits;
        }

        private bool IsUnderDirectory(string path, string directory)
        {
            try
            {
                string fullPath = Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                string fullDir = Path.GetFullPath(directory).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                return fullPath.Equals(fullDir, StringComparison.OrdinalIgnoreCase)
                    || fullPath.StartsWith(fullDir + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
                    || fullPath.StartsWith(fullDir + Path.AltDirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);
            }
            catch
            {
                return false;
            }
        }

        private bool TryGetMap(Dictionary<string, object> data, string key, out Dictionary<string, object> map)
        {
            map = null;
            if (!data.TryGetValue(key, out object value)) return false;
            map = value as Dictionary<string, object>;
            return map != null;
        }

        private Dictionary<string, object> ReadJsonObject(string path)
        {
            using JsonDocument document = JsonDocument.Parse(File.ReadAllText(path, Encoding.UTF8));
            return JsonDocumentToObject(document);
        }

        private Dictionary<string, object> JsonTextToObject(string json)
        {
            using JsonDocument document = JsonDocument.Parse(json);
            return JsonDocumentToObject(document);
        }

        private Dictionary<string, object> JsonDocumentToObject(JsonDocument document)
        {
            var output = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            if (document.RootElement.ValueKind != JsonValueKind.Object) return output;
            foreach (JsonProperty property in document.RootElement.EnumerateObject())
            {
                output[property.Name] = JsonValueToObject(property.Value);
            }
            return output;
        }

        private object JsonValueToObject(JsonElement element)
        {
            switch (element.ValueKind)
            {
                case JsonValueKind.String: return element.GetString() ?? "";
                case JsonValueKind.Number:
                    return element.TryGetInt64(out long n) ? n : element.GetDouble();
                case JsonValueKind.True: return true;
                case JsonValueKind.False: return false;
                case JsonValueKind.Object:
                    var obj = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                    foreach (JsonProperty property in element.EnumerateObject()) obj[property.Name] = JsonValueToObject(property.Value);
                    return obj;
                case JsonValueKind.Array:
                    return element.EnumerateArray().Select(JsonValueToObject).ToList();
                default: return "";
            }
        }

        private string GetString(Dictionary<string, object> data, string key)
        {
            return data.TryGetValue(key, out object value) && value != null ? Convert.ToString(value) ?? "" : "";
        }

        private string DisplayText(object value)
        {
            if (value is ComboBoxItem item) return Convert.ToString(item.Content) ?? "";
            return Convert.ToString(value) ?? "";
        }

        private string JoinArgs(List<string> args) => string.Join(" ", args.Select(Quote));

        private string Quote(string value)
        {
            value ??= "";
            return value.IndexOfAny(new[] { ' ', '\t', '"', '&', '|' }) < 0 ? value : "\"" + value.Replace("\"", "\\\"") + "\"";
        }

        private string Mask(string value)
        {
            value = (value ?? "").Trim();
            return value.Length <= 12 ? value : value.Substring(0, 6) + "..." + value.Substring(value.Length - 4);
        }

        private string SafeTime(DateTime time) => time.ToString("yyyy-MM-dd HH:mm:ss");

        private void OpenPath(string path)
        {
            try
            {
                if (File.Exists(path) || Directory.Exists(path))
                {
                    if (File.Exists(path) && ShouldOpenWithNotepad(path))
                    {
                        OpenWithNotepad(path);
                        return;
                    }
                    Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
                    return;
                }
                if (Path.GetExtension(path).Length > 0)
                {
                    string directory = Path.GetDirectoryName(Path.GetFullPath(path)) ?? rootDir;
                    Directory.CreateDirectory(directory);
                    string example = Path.Combine(rootDir, "config.example.json");
                    if (Path.GetFileName(path).Equals("config.json", StringComparison.OrdinalIgnoreCase) && File.Exists(example))
                    {
                        File.Copy(example, path);
                    }
                    else if (!File.Exists(path))
                    {
                        File.WriteAllText(path, "", Encoding.UTF8);
                    }
                    OpenWithNotepad(path);
                    return;
                }
                Directory.CreateDirectory(path);
                Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                Log("打开失败：" + ex.Message);
            }
        }

        private bool ShouldOpenWithNotepad(string path)
        {
            string extension = Path.GetExtension(path).ToLowerInvariant();
            return extension == ".json" || extension == ".txt" || extension == ".log";
        }

        private void OpenWithNotepad(string path)
        {
            var psi = new ProcessStartInfo("notepad.exe")
            {
                UseShellExecute = false
            };
            psi.ArgumentList.Add(path);
            Process.Start(psi);
        }

        private void OpenUrl(string url)
        {
            try
            {
                if (!Uri.TryCreate(url, UriKind.Absolute, out Uri uri) ||
                    (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
                {
                    Log("无效链接：" + url);
                    return;
                }
                Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                Log("打开链接失败：" + ex.Message);
            }
        }

        private void OpenPayPalUrl(string url, string accountEmail = "")
        {
            if (!IsHttpUrl(url))
            {
                Log("无效支付链接：" + url);
                return;
            }
            string chrome = FindChromePath();
            if (chrome.Length == 0)
            {
                Log("未找到 Chrome，使用系统默认浏览器打开支付链接。");
                OpenUrl(url);
                return;
            }
            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = chrome,
                    UseShellExecute = false
                };
                psi.ArgumentList.Add("--new-window");
                psi.ArgumentList.Add("--incognito");
                psi.ArgumentList.Add(url);
                Process.Start(psi);
                Log("已用 Chrome 无痕窗口打开支付链接。");
            }
            catch (Exception ex)
            {
                Log("Chrome 打开失败：" + ex.Message);
                OpenUrl(url);
            }
        }

        private void CopyPayPalUrl(string url)
        {
            if (!IsHttpUrl(url))
            {
                Log("无效支付链接，无法复制。");
                return;
            }
            try
            {
                Clipboard.SetText(url);
                Log("支付链接已复制。");
            }
            catch (Exception ex)
            {
                Log("复制支付链接失败：" + ex.Message);
            }
        }

        private string FindChromePath()
        {
            string[] candidates =
            {
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Google", "Chrome", "Application", "chrome.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), "Google", "Chrome", "Application", "chrome.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Google", "Chrome", "Application", "chrome.exe")
            };
            return candidates.FirstOrDefault(File.Exists) ?? "";
        }

        private bool IsHttpUrl(string url)
        {
            return Uri.TryCreate(url, UriKind.Absolute, out Uri uri)
                && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps);
        }

        private void ClearLog_Click(object sender, RoutedEventArgs e)
        {
            LogText = "";
        }

        private void Log(string text)
        {
            AppServices.Logger?.Information(text);
            LogText += "[" + DateTime.Now.ToString("HH:mm:ss") + "] " + text + Environment.NewLine;
        }

        private void UiLog(string text)
        {
            AppServices.Logger?.Debug("[backend] {Line}", text);
            Dispatcher.BeginInvoke(new Action(() => Log(text)), DispatcherPriority.Background);
        }

        // ── HandyControl Growl notification helpers (P5) ──

        private static void NotifySuccess(string message)
        {
            Dispatcher.CurrentDispatcher.BeginInvoke(new Action(() =>
                HandyControl.Controls.Growl.Success(message)), DispatcherPriority.Normal);
        }

        private static void NotifyWarning(string message)
        {
            Dispatcher.CurrentDispatcher.BeginInvoke(new Action(() =>
                HandyControl.Controls.Growl.Warning(message)), DispatcherPriority.Normal);
        }

        private static void NotifyInfo(string message)
        {
            Dispatcher.CurrentDispatcher.BeginInvoke(new Action(() =>
                HandyControl.Controls.Growl.Info(message)), DispatcherPriority.Normal);
        }

        private void OnPropertyChanged(string name)
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
        }
    }
}
