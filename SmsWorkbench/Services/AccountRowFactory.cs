// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// Backend account JSON -> <see cref="PoolRow"/>.
    /// </summary>
    /// <remarks>
    /// Split from <c>MainWindow.Pools.cs</c> (2026-11-01). It reads a dictionary the
    /// desktop-read channel already produced and touches no window state, so it is a
    /// static mapper: the pool partial decides *when* to build a row, this decides
    /// *what* one looks like. The identity block rendering lives here too, which is
    /// why it can be unit-tested without opening a window.
    /// </remarks>
    public static class AccountRowFactory
    {
        public static PoolRow BuildBackendAccountRow(Dictionary<string, object> data, string databasePath, string rawJson)
        {
            string status = BackendJson.GetString(data, "status");
            bool hasAccess = BackendJson.ParseBoolean(BackendJson.FirstNonEmpty(
                BackendJson.GetString(data, "access_token_present"),
                BackendJson.GetString(data, "has_access_token")));
            bool hasPaymentUrl = BackendJson.ParseBoolean(BackendJson.FirstNonEmpty(
                BackendJson.GetString(data, "payment_url_present"),
                BackendJson.GetString(data, "has_payment_url")));
            string accessState = hasAccess ? "present" : "";
            string paymentMethod = BackendJson.GetString(data, "payment_method");
            string paypalUrl = hasPaymentUrl ? "backend://payment-url" : "";
            string paypalStatus = BackendJson.GetString(data, "paypal_status");
            string paypalOk = BackendJson.GetString(data, "paypal_ok");
            string refreshStatus = BackendJson.GetString(data, "refresh_token_status");
            if (BackendJson.ParseBoolean(BackendJson.FirstNonEmpty(
                    BackendJson.GetString(data, "refresh_token_present"),
                    BackendJson.GetString(data, "has_refresh_token")))
                && (refreshStatus.Length == 0 || refreshStatus.Equals("no_rt", StringComparison.OrdinalIgnoreCase)))
            {
                refreshStatus = "oauth_present";
            }
            string provider = BackendJson.GetString(data, "mailbox_provider");
            // `rawJson` is the full session blob (tens of KB per account) and both
            // GetImportedStatus / GetPaypalAmount parse it from scratch via
            // BackendJson.TextToObject. They used to be called twice each in the
            // initialiser below (GetPaypalAmount at both the PayPalAmount and the
            // PromotionStatus member, DisplayPayPalStatus likewise), i.e. 3 full
            // JSON parses per row where 1 suffices - and this runs once per
            // account on every pool refresh. Hoisted here so each is parsed once.
            string importedStatus = BackendJson.FirstNonEmpty(
                BackendJson.GetString(data, "imported_status"),
                AccountStatusInterpreter.GetImportedStatus(rawJson));
            string paypalAmount = BackendJson.FirstNonEmpty(
                BackendJson.GetString(data, "paypal_amount"),
                AccountStatusInterpreter.GetPaypalAmount(rawJson));
            // Pure function of its arguments, but it was evaluated twice with
            // identical arguments (PayPalStatus member and PromotionStatus member).
            string paypalStatusDisplay = AccountStatusInterpreter.DisplayPayPalStatus(paypalStatus, paypalOk, paypalUrl, paymentMethod);
            (string fingerprintProfile, string egressEndpoint, string egressCountry, bool identityBound) = ReadIdentity(data);
            var row = new PoolRow
            {
                Id = "DB" + BackendJson.GetString(data, "id"),
                CreatedAt = UnixTimeText(BackendJson.GetString(data, "created_at")),
                CompletedAt = UnixTimeText(BackendJson.GetString(data, "updated_at")),
                Identifier = BackendJson.GetString(data, "email"),
                AccountType = MailboxTypeDisplay(provider, BackendJson.GetString(data, "email")),
                AccountPlanType = AccountStatusInterpreter.GetAccountPlanType(data),
                Source = BackendJson.GetString(data, "source"),
                RegisterMethod = BackendJson.GetString(data, "register_method"),
                SessionType = BackendJson.GetString(data, "session_type"),
                PlanType = BackendJson.FirstNonEmpty(BackendJson.GetString(data, "plan_type"), BackendJson.GetString(data, "account_type")),
                RegistrationCountry = BackendJson.GetString(data, "registration_country"),
                FingerprintProfile = fingerprintProfile,
                EgressEndpoint = egressEndpoint,
                EgressCountry = egressCountry,
                IdentityBound = identityBound,
                Status = AccountStatusInterpreter.DisplayAccountStatus(status, paypalOk, accessState, BackendJson.GetString(data, "error"), paypalStatus, refreshStatus, importedStatus),
                RegistrationStatus = BackendJson.FirstNonEmpty(BackendJson.GetString(data, "registration_status"), BackendJson.GetString(data, "registration_state")),
                PayPalStatus = paypalStatusDisplay,
                PayPalAmount = paypalAmount,
                // Python owns the display composition and sends it as
                // `promotion_display`; rows from an older backend carry only
                // `promotion_status`, hence the fallback.
                PromotionStatus = AccountStatusInterpreter.DisplayPromotionStatus(
                    BackendJson.FirstNonEmpty(
                        BackendJson.GetString(data, "promotion_display"),
                        BackendJson.GetString(data, "promotion_status")),
                    paypalStatusDisplay,
                    paypalAmount),
                PromotionState = BackendJson.GetString(data, "promotion_state"),
                // Python composes the 支付资格 badge (payment_qualification_display);
                // the state literal is the stable machine value. An older backend
                // or a never-probed account leaves both blank.
                PaymentQualification = AccountStatusInterpreter.DisplayPaymentQualification(
                    BackendJson.GetString(data, "payment_qualification_display")),
                PaymentQualificationState = BackendJson.GetString(data, "payment_qualification_state"),
                RefreshTokenStatus = AccountStatusInterpreter.DisplayRtStatus(refreshStatus),
                TwoFactorStatus = AccountStatusInterpreter.HasTwoFactor(data) ? Loc.T("已设置") : Loc.T("未设置"),
                HasAccessToken = hasAccess,
                AccessTokenProbeStatusCode = AccountStatusInterpreter.GetAccessTokenProbeStatusCode(data),
                PayPalUrl = paypalUrl,
                RefreshToken = provider == "remail" ? "ReMail" : hasAccess ? "AT" : "",
                Proxy = DbTimingText(new Dictionary<string, string>(data.ToDictionary(pair => pair.Key, pair => Convert.ToString(pair.Value) ?? ""))),
                Notes = BackendJson.GetString(data, "json_path").Length > 0 ? BackendJson.GetString(data, "json_path") : databasePath,
                SourcePath = databasePath,
                RawLine = BackendJson.GetString(data, "id"),
                MailboxProvider = provider
            };
            return row;
        }

        /// <summary>
        /// Unpack the backend's <c>identity</c> block (egress + fingerprint provenance).
        /// <para>
        /// Rendered as one <c>host:port</c> string rather than five fields: the
        /// detail panel has a single row for it, and the scheme/port split is of no
        /// use to an operator. <c>bound</c> distinguishes "no recorded exit" from an
        /// exit whose host was empty, which the panel renders as an em dash.
        /// </para>
        /// </summary>
        private static (string FingerprintProfile, string Egress, string Country, bool Bound) ReadIdentity(
            Dictionary<string, object> data)
        {
            if (!BackendJson.TryGetMap(data, "identity", out Dictionary<string, object>? identity))
                return ("", "", "", false);

            string scheme = BackendJson.GetString(identity, "proxy_scheme");
            string host = BackendJson.GetString(identity, "proxy_host");
            string port = BackendJson.GetString(identity, "proxy_port");
            bool hasPort = port.Length > 0 && port != "0";
            string egress = host.Length == 0
                ? ""
                : (scheme.Length > 0 ? scheme + "://" : "") + host + (hasPort ? ":" + port : "");
            return (
                BackendJson.GetString(identity, "fingerprint_profile"),
                egress,
                BackendJson.GetString(identity, "proxy_country"),
                BackendJson.ParseBoolean(BackendJson.GetString(identity, "bound")));
        }

        public static string MailboxTypeDisplay(string provider, string email = "")
        {
            string normalized = (provider ?? "").Trim().ToLowerInvariant().Replace("-", "_");
            string domain = (email ?? "").Split('@').LastOrDefault()?.ToLowerInvariant() ?? "";
            return normalized switch
            {
                "remail" when domain is "outlook.com" or "hotmail.com" or "live.com" or "msn.com" => "remail/outlook",
                "remail" when domain == "icloud.com" => "remail/icloud",
                "remail" => "remail",
                "icloud_url" or "icloud" => "icloud",
                "cf_worker" or "cfworker" => "cfworker",
                "microsoft" or "graph" or "outlook" => "outlook",
                "gmail" => "gmail",
                "smailr" => "smailr",
                "chatai" => "chatai",
                "" => "unknown",
                _ => normalized,
            };
        }

        // Moved with the row builder: both were private window helpers whose only
        // callers were inside BuildBackendAccountRow, so they belong to the mapper.
        private static string DbTimingText(Dictionary<string, string> data)
        {
            string pipeline = data.TryGetValue("pipeline_total_seconds", out string? pipelineSeconds) ? pipelineSeconds : "";
            if (!string.IsNullOrWhiteSpace(pipeline) && pipeline != "0.0" && pipeline != "0") return pipeline + "s";
            string timing = data.TryGetValue("timing_total_seconds", out string? timingSeconds) ? timingSeconds : "";
            return string.IsNullOrWhiteSpace(timing) || timing == "0.0" || timing == "0" ? "" : timing + "s";
        }

        private static string UnixTimeText(string raw)
        {
            if (!long.TryParse(raw, out long seconds) || seconds <= 0) return "";
            return DateTimeOffset.FromUnixTimeSeconds(seconds).LocalDateTime.ToString("yyyy-MM-dd HH:mm:ss");
        }
    }
}
