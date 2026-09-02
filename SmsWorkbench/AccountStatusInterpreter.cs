// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs. Worth doing here in particular: this class is the one
// business-rule implementation for account status and had no test coverage at
// all, so "dictionary may not contain this key" is exactly the class of bug that
// goes unnoticed in it.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Window-independent interpretation of backend account JSON: plan type,
    /// quota labels, payment status, deactivation detection, and import state.
    /// This is the single business-rule implementation that previously lived
    /// inside MainWindow.Helpers in parallel with ProtocolPaymentResultPresenter.
    /// All methods operate on the generic dictionaries produced by
    /// <see cref="BackendJson"/>.
    /// </summary>
    public static class AccountStatusInterpreter
    {
        public static string GetAccountPlanType(Dictionary<string, object> data)
        {
            if (data == null) return "free";
            string k12Status = BackendJson.FirstNonEmpty(
                BackendJson.GetString(data, "k12_status"),
                BackendJson.NestedString(data, "k12", "status"),
                BackendJson.NestedString(data, "workspace_scan", "account_type_after"),
                BackendJson.NestedString(data, "account_scan", "workspace", "account_type_after")
            ).Trim().ToLowerInvariant();
            if ((k12Status.Contains("k12") || BackendJson.GetString(data, "k12_workspace_id").Length > 0 || BackendJson.NestedString(data, "k12", "workspace_id").Length > 0)
                && !k12Status.Contains("left")
                && !k12Status.Contains("fallback_free"))
            {
                return "k12";
            }

            string value = BackendJson.FirstNonEmpty(
                BackendJson.GetString(data, "subscription_type"),
                BackendJson.GetString(data, "plan_type"),
                BackendJson.GetString(data, "planType"),
                BackendJson.NestedString(data, "account", "plan_type"),
                BackendJson.NestedString(data, "account", "planType"),
                BackendJson.NestedString(data, "auth_session", "account", "plan_type"),
                BackendJson.NestedString(data, "auth_session", "account", "planType"),
                BackendJson.JwtAuthString(BackendJson.GetString(data, "access_token"), "chatgpt_plan_type"),
                BackendJson.JwtAuthString(BackendJson.GetString(data, "access_token"), "plan_type"),
                BackendJson.GetString(data, "account_type")
            ).Trim().ToLowerInvariant();

            if (value.Contains("pro")) return "pro";
            if (value.Contains("team") || value.Contains("business") || value.Contains("enterprise")) return "team";
            if (value.Contains("k12") || value.Contains("edu")) return "k12";
            if (value.Contains("plus")) return "plus";
            return "free";
        }

        // GetQuotaStatus() removed (2026-09-02, round 6): zero callers and zero
        // tests. It was the only consumer of FormatWhamUsageLabel /
        // ExtractWhamUsage, so those are now unreferenced too and are removed
        // with it. The quota keys it read (quota_status, usage.remaining/limit,
        // auth_session.account.quota_status) are still written by the Python
        // side; revive this shape if quota display comes back.

        public static string GetAccessTokenProbeStatusCode(Dictionary<string, object> data)
        {
            if (data == null) return "";
            string explicitCode = BackendJson.FirstNonEmpty(
                BackendJson.GetString(data, "at_probe_status_code"),
                BackendJson.GetString(data, "access_token_probe_status_code"),
                BackendJson.NestedString(data, "account_scan", "token_probe", "status_code"),
                BackendJson.NestedString(data, "quota", "last_result", "status_code"),
                BackendJson.NestedString(data, "token_probe", "status_code"),
                BackendJson.NestedString(data, "scan", "token_probe", "status_code"),
                BackendJson.NestedString(data, "session", "account_scan", "token_probe", "status_code"),
                BackendJson.NestedString(data, "session", "quota", "last_result", "status_code"),
                BackendJson.NestedString(data, "session", "token_probe", "status_code"),
                BackendJson.NestedString(data, "session", "scan", "token_probe", "status_code")
            ).Trim();
            return AccessTokenState.ResolveProbeStatusCode(
                explicitCode,
                BackendJson.FirstNonEmpty(BackendJson.GetString(data, "status"), BackendJson.NestedString(data, "session", "status")),
                BackendJson.FirstNonEmpty(BackendJson.GetString(data, "error"), BackendJson.NestedString(data, "session", "error")));
        }

        // ExtractWhamUsage / FormatWhamUsageLabel removed (2026-09-02, round 6).
        // They existed only to feed the quota label produced by GetQuotaStatus,
        // which itself had no callers. The Python side still writes
        // quota.last_result.wham_usage, so re-adding quota display means
        // reviving these three together.

        // FmtTokenCount removed (2026-09-02, round 6): its only caller was
        // FormatWhamUsageLabel, removed above.

        public static bool IsPaymentLinkMethodMismatch(string rawJson, string paymentMethod)
        {
            if (string.IsNullOrWhiteSpace(rawJson)) return false;
            try
            {
                return IsPaymentLinkMethodMismatch(BackendJson.TextToObject(rawJson), paymentMethod);
            }
            catch
            {
                return false;
            }
        }

        public static bool IsPaymentLinkMethodMismatch(Dictionary<string, object> data, string paymentMethod)
        {
            string requested = PaymentMethods.Normalize(paymentMethod);
            if (!BackendJson.TryGetMap(data, "paypal", out Dictionary<string, object>? paypal) || paypal.Count == 0) return false;
            string savedMethod = PaymentMethods.Normalize(BackendJson.FirstNonEmpty(
                BackendJson.GetString(paypal, "payment_method"),
                BackendJson.GetString(paypal, "method"),
                BackendJson.GetString(paypal, "type")
            ));
            bool hasSavedMethod = BackendJson.GetString(paypal, "payment_method").Length > 0
                || BackendJson.GetString(paypal, "method").Length > 0
                || BackendJson.GetString(paypal, "type").Length > 0;
            string currency = BackendJson.GetString(paypal, "currency").Trim().ToLowerInvariant();
            string detected = hasSavedMethod ? savedMethod : "";
            if (detected.Length == 0)
            {
                foreach (string candidate in new[] { "paypal", "gopay", "gcash", "grabpay", "upi", "ideal", "pix", "kakao", "blik", "twint", "momo" })
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
                    "php" when requested is "gcash" or "grabpay" or "direct_card" => requested,
                    "inr" => "upi",
                    "brl" => "pix",
                    "krw" => "kakao",
                    "pln" => "blik",
                    "chf" => "twint",
                    "vnd" => "momo",
                    "eur" when requested == "ideal" => "ideal",
                    "usd" => "paypal",
                    _ => ""
                };
            }
            return detected.Length > 0 && detected != requested;
        }

        private static bool PaymentMethodTypesContain(Dictionary<string, object> paypal, string expected)
        {
            if (!paypal.TryGetValue("payment_method_types", out object? raw) || raw == null) return false;
            string target = expected.Trim().ToLowerInvariant();
            if (raw is List<object> items)
            {
                return items.Any(item => string.Equals(Convert.ToString(item, CultureInfo.InvariantCulture)?.Trim(), target, StringComparison.OrdinalIgnoreCase));
            }
            return Convert.ToString(raw, CultureInfo.InvariantCulture)?.IndexOf(target, StringComparison.OrdinalIgnoreCase) >= 0;
        }

        public static string DisplayAccountStatus(string status, string paypalOk, string access, string error, string paypalStatus, string refreshTokenStatus, string importedStatus)
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

        public static bool LooksAtInvalidError(string error)
        {
            string text = (error ?? "").ToLowerInvariant();
            foreach (string marker in BackendTextMarkers.AtInvalid)
            {
                if (text.Contains(marker, StringComparison.Ordinal)) return true;
            }
            return LooksAccountDeactivatedError(text);
        }

        public static bool LooksPhoneVerificationError(string error)
        {
            string text = (error ?? "").ToLowerInvariant();
            return text.Contains("secondary_phone_verification_required", StringComparison.Ordinal)
                || text.Contains("add_phone_required", StringComparison.Ordinal);
        }

        public static bool LooksAccountDeactivatedError(string error)
        {
            string text = (error ?? "").ToLowerInvariant();
            foreach (string marker in BackendTextMarkers.AccountDeactivated)
            {
                if (text.Contains(marker, StringComparison.Ordinal)) return true;
            }
            return false;
        }

        public static string DisplayPayPalStatus(string paypalStatus, string paypalOk, string paypalUrl, string paymentMethod = "")
        {
            string prefix = PaymentMethods.Normalize(paymentMethod) == "paypal" ? "" : PaymentMethods.DisplayName(paymentMethod) + " ";
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

        public static string PaymentPendingStatus(string paymentMethod)
        {
            return PaymentMethods.DisplayName(paymentMethod) + "待支付";
        }

        /// <summary>
        /// Unified 优惠状态 shown after merging the former 支付状态 + 支付金额 columns.
        /// Prefers the account plan/promotion probe (accounts/check) result; when it
        /// has not been run, falls back to the payment link status combined with its
        /// amount so no information from the old two columns is lost.
        /// </summary>
        public static string DisplayPromotionStatus(string promotionStatus, string payPalStatus, string payPalAmount)
        {
            string promotion = (promotionStatus ?? "").Trim();
            if (promotion.Length > 0) return promotion;
            string status = (payPalStatus ?? "").Trim();
            string amount = (payPalAmount ?? "").Trim();
            if (status.Length > 0 && amount.Length > 0) return status + " · " + amount;
            if (status.Length > 0) return status;
            return amount;
        }

        public static string DisplayRtStatus(string refreshTokenStatus)
        {
            string value = (refreshTokenStatus ?? "").Trim();
            return value.Equals("oauth_present", StringComparison.OrdinalIgnoreCase)
                || value.Equals("legacy_present", StringComparison.OrdinalIgnoreCase)
                ? "已获取"
                : "未获取";
        }

        public static string GetImportedStatus(string rawJson)
        {
            if (string.IsNullOrWhiteSpace(rawJson)) return "";
            try
            {
                return GetImportedStatus(BackendJson.TextToObject(rawJson));
            }
            catch
            {
                return "";
            }
        }

        public static string GetImportedStatus(Dictionary<string, object> data)
        {
            bool cpaImported = IsImportOk(data, "cpa_import");
            bool sub2Imported = IsImportOk(data, "sub2api_import");
            if (cpaImported && sub2Imported) return "已导入CPA/SUB2";
            if (cpaImported) return "已导入CPA";
            if (sub2Imported) return "已导入SUB2";
            return "";
        }

        private static bool IsImportOk(Dictionary<string, object> data, string key)
        {
            if (!BackendJson.TryGetMap(data, key, out Dictionary<string, object>? importData)) return false;
            return BackendJson.GetString(importData, "ok").Equals("true", StringComparison.OrdinalIgnoreCase);
        }

        public static string GetPaypalAmount(string rawJson)
        {
            if (string.IsNullOrWhiteSpace(rawJson)) return "";
            try
            {
                return GetPaypalAmount(BackendJson.TextToObject(rawJson));
            }
            catch
            {
                return "";
            }
        }

        public static string GetPaypalAmount(Dictionary<string, object> data)
        {
            if (!BackendJson.TryGetMap(data, "paypal", out Dictionary<string, object>? paypal)) return "";
            string currency = BackendJson.GetString(paypal, "currency").Trim().ToUpperInvariant();
            string rawAmount = BackendJson.FirstNonEmpty(
                BackendJson.GetString(paypal, "amount_due"),
                BackendJson.GetString(paypal, "due"),
                BackendJson.GetString(paypal, "expected_amount")
            );
            if (rawAmount.Length == 0) return "";
            if (!decimal.TryParse(rawAmount, out decimal amount)) return currency.Length > 0 ? rawAmount + " " + currency : rawAmount;
            decimal displayAmount = amount / 100m;
            string text = displayAmount.ToString("0.00", CultureInfo.InvariantCulture);
            return currency.Length > 0 ? text + " " + currency : text;
        }

        public static bool HasTwoFactor(Dictionary<string, object> data)
        {
            if (BackendJson.ParseBoolean(BackendJson.FirstNonEmpty(
                    BackendJson.GetString(data, "totp_present"),
                    BackendJson.GetString(data, "totp_enrolled"),
                    BackendJson.GetString(data, "has_totp"))))
            {
                return true;
            }
            if (!string.IsNullOrWhiteSpace(BackendJson.GetString(data, "totp_secret"))) return true;
            return long.TryParse(BackendJson.GetString(data, "twofa_enrolled_at"), out long enrolledAt) && enrolledAt > 0;
        }
    }
}
