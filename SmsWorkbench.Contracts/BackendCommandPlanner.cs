namespace SmsWorkbench
{
    /// <summary>
    /// A fully resolved backend invocation: task label, CLI arguments, optional
    /// per-command environment values, any temporary files the planner created,
    /// and an optional timeout override for the caller that executes the plan.
    /// </summary>
    public sealed record BackendCommandPlan(
        string TaskName,
        IReadOnlyList<string> Arguments,
        IReadOnlyDictionary<string, string> EnvironmentVariables = null,
        IReadOnlyList<string> TemporaryFiles = null,
        int? TimeoutMilliseconds = null)
    {
        public IReadOnlyDictionary<string, string> Environment { get; } = EnvironmentVariables
            ?? new Dictionary<string, string>();

        public IReadOnlyList<string> TempFiles { get; } = TemporaryFiles ?? Array.Empty<string>();
    }

    /// <summary>
    /// Window-independent planner for every desktop backend command family
    /// (registration, SMS, liveness, deletion, import, export, refresh,
    /// protocol payment, inbox). It generalizes the
    /// <see cref="ProtocolPaymentExecutionPlanner"/> pattern so the CLI
    /// contract lives in exactly one module that can be unit tested by every
    /// client adapter. Settings resolution stays with the caller; this class only shapes
    /// already-resolved values into command-line arguments.
    /// </summary>
    public static class BackendCommandPlanner
    {
        private static readonly UTF8Encoding Utf8NoBom = new(false);

        // ── Registration ────────────────────────────────────────────────

        public static BackendCommandPlan CreatePoolRegistration(
            int count,
            IReadOnlyList<string> proxyPool,
            int workers = 4)
        {
            var args = new List<string>
            {
                "--count", Count(count),
                "--workers", Count(workers),
            };
            AppendNoPhoneReuse(args);
            AppendProxyPool(args, proxyPool);
            return new BackendCommandPlan("邮箱池注册", args);
        }

        /// <summary>
        /// Mailbox-file registration shared by the selected-mailbox,
        /// unregistered-mailbox, one-click pool, and failed-rerun actions.
        /// </summary>
        public static BackendCommandPlan CreateMailboxFileRegistration(
            string taskName,
            string mailboxArgument,
            string mailboxFile,
            int count,
            int workers,
            bool registrationAtOnly,
            IReadOnlyList<string> proxyPool,
            bool disable2fa = false,
            bool checkPromotion = false)
        {
            RequireArgument(mailboxArgument, nameof(mailboxArgument));
            RequireArgument(mailboxFile, nameof(mailboxFile));
            var args = new List<string>
            {
                mailboxArgument, mailboxFile,
                "--count", Count(count),
                "--workers", Count(workers),
            };
            if (registrationAtOnly) args.Add("--registration-at-only");
            AppendNoPhoneReuse(args);
            AppendNo2fa(args, disable2fa);
            AppendCheckPromotion(args, checkPromotion);
            AppendProxyPool(args, proxyPool);
            return new BackendCommandPlan(taskName, args);
        }

        public static BackendCommandPlan CreateRerunFailedRegistration(
            string mailboxArgument,
            string mailboxFile,
            int count,
            IReadOnlyList<string> proxyPool)
        {
            return CreateMailboxFileRegistration(
                "重新注册失败账号 (" + Count(count) + ")",
                mailboxArgument,
                mailboxFile,
                count,
                4,
                registrationAtOnly: false,
                proxyPool);
        }

        public static BackendCommandPlan CreatePhoneRegistration(
            int count,
            IReadOnlyList<string> proxyPool,
            bool disable2fa = false,
            bool checkPromotion = false)
        {
            var args = new List<string> { "--phone-register", "--count", Count(count) };
            AppendNo2fa(args, disable2fa);
            AppendCheckPromotion(args, checkPromotion);
            AppendProxyPool(args, proxyPool);
            return new BackendCommandPlan("手机号注册 (SMSBower)", args);
        }

        public static BackendCommandPlan CreateCfWorkerRegistration(
            string domain,
            int count,
            int workers,
            IReadOnlyList<string> proxyPool,
            bool disable2fa = false,
            bool checkPromotion = false)
        {
            var args = new List<string>
            {
                "--buy-cfworker-mailbox",
                "--cfworker-domain", RequireArgument(domain, nameof(domain)),
                "--count", Count(count),
                "--workers", Count(workers),
            };
            AppendRegistrationAtOnly(args);
            AppendNo2fa(args, disable2fa);
            AppendCheckPromotion(args, checkPromotion);
            AppendProxyPool(args, proxyPool);
            return new BackendCommandPlan("CFWorker邮箱注册", args);
        }

        public static BackendCommandPlan CreateRemailTargetRegistration(
            int count,
            int workers,
            IReadOnlyList<string> proxyPool,
            bool disable2fa = false,
            bool checkPromotion = false)
        {
            var args = new List<string>
            {
                "--target-at200", Count(count),
                "--buy-remail-mailbox",
                "--remail-service-mode", "purchase",
                "--workers", Count(workers),
            };
            AppendNoPhoneReuse(args);
            AppendNo2fa(args, disable2fa);
            AppendCheckPromotion(args, checkPromotion);
            AppendProxyPool(args, proxyPool);
            return new BackendCommandPlan("ReMail 长效邮箱注册 (" + Count(count) + ")", args);
        }

        public static BackendCommandPlan CreateSmailrRegistration(
            string domain,
            int count,
            int workers,
            IReadOnlyList<string> proxyPool,
            bool disable2fa = false,
            bool checkPromotion = false)
        {
            var args = new List<string>
            {
                "--buy-smailr-mailbox",
                "--smailr-domain", RequireArgument(domain, nameof(domain)),
                "--count", Count(count),
                "--workers", Count(workers),
            };
            AppendRegistrationAtOnly(args);
            AppendNo2fa(args, disable2fa);
            AppendCheckPromotion(args, checkPromotion);
            AppendProxyPool(args, proxyPool);
            return new BackendCommandPlan("Smailr 邮箱注册", args);
        }

        // ── One-click SMS (接码) ────────────────────────────────────────

        public static BackendCommandPlan CreateOneClickSms(
            string mailboxArgument,
            string mailboxFile,
            IReadOnlyList<string> emails,
            string sessionFile,
            IReadOnlyList<string> proxyPool,
            string tempDirectory = null)
        {
            RequireArgument(mailboxArgument, nameof(mailboxArgument));
            RequireArgument(mailboxFile, nameof(mailboxFile));
            List<string> targets = RequireEmails(emails);
            var args = new List<string>
            {
                "--one-click-sms",
                "--phone-source", "smsbower",
                "--workers", "1",
                "--refresh-timeout", "60",
                mailboxArgument, mailboxFile,
            };
            var tempFiles = new List<string>();
            if (targets.Count > 1)
            {
                string emailFile = WriteEmailFile(tempDirectory, "oneclick_sms_emails_", targets);
                tempFiles.Add(emailFile);
                args.AddRange(new[] { "--email-file", emailFile });
            }
            else
            {
                args.AddRange(new[] { "--email", targets[0] });
                AppendSessionFile(args, sessionFile);
            }
            AppendProxyPool(args, proxyPool);
            return new BackendCommandPlan(
                "一键接码(" + Count(targets.Count) + ")",
                args,
                TemporaryFiles: tempFiles);
        }

        // ── Account liveness (测活) ─────────────────────────────────────

        public static BackendCommandPlan CreateAccountScan(
            IReadOnlyList<string> emails,
            string sessionFile,
            int workers,
            bool autoRelogin,
            IReadOnlyList<string> proxyPool,
            string tempDirectory = null)
        {
            List<string> targets = RequireEmails(emails);
            var args = new List<string>
            {
                "--refresh-local-quota",
                "--quota-workers", Count(workers),
                "--refresh-timeout", "90",
            };
            if (autoRelogin)
            {
                args.Add("--quota-auto-relogin");
                args.Add("--quota-relogin-timeout");
                args.Add("300");
            }
            var tempFiles = new List<string>();
            if (targets.Count > 1)
            {
                string emailFile = WriteEmailFile(tempDirectory, "oneclick_scan_emails_", targets);
                tempFiles.Add(emailFile);
                args.AddRange(new[] { "--email-file", emailFile });
            }
            else
            {
                args.AddRange(new[] { "--email", targets[0] });
                AppendSessionFile(args, sessionFile);
            }
            AppendProxyPool(args, proxyPool);
            return new BackendCommandPlan(
                "账号测活(" + Count(targets.Count) + ")",
                args,
                TemporaryFiles: tempFiles);
        }

        public static BackendCommandPlan CreateChangeEmail(
            IReadOnlyList<string> emails,
            string provider,
            string mailboxFile,
            int workers,
            string smailrDomain,
            string cfworkerDomain,
            IReadOnlyList<string> proxyPool,
            string tempDirectory = null)
        {
            List<string> targets = RequireEmails(emails);
            RequireArgument(provider, nameof(provider));
            var args = new List<string>
            {
                "--change-email",
                "--change-email-provider", provider.Trim().ToLowerInvariant(),
                "--change-email-workers", Count(workers),
            };
            var tempFiles = new List<string>();
            string emailFile = WriteEmailFile(tempDirectory, "change_email_accounts_", targets);
            tempFiles.Add(emailFile);
            args.AddRange(new[] { "--email-file", emailFile });
            if (!string.IsNullOrWhiteSpace(mailboxFile))
                args.AddRange(new[] { "--change-email-mailbox-file", mailboxFile.Trim() });
            if (!string.IsNullOrWhiteSpace(smailrDomain))
                args.AddRange(new[] { "--change-email-smailr-domain", smailrDomain.Trim() });
            if (!string.IsNullOrWhiteSpace(cfworkerDomain))
                args.AddRange(new[] { "--cfworker-domain", cfworkerDomain.Trim() });
            AppendProxyPool(args, proxyPool);
            return new BackendCommandPlan("批量邮箱换绑(" + Count(targets.Count) + ")", args, TemporaryFiles: tempFiles, TimeoutMilliseconds: 900000);
        }

        public static BackendCommandPlan CreatePromotionCheck(
            IReadOnlyList<string> emails,
            int workers,
            IReadOnlyList<string> proxyPool,
            string tempDirectory = null)
        {
            List<string> targets = RequireEmails(emails);
            var args = new List<string>
            {
                "--check-promotion",
                "--quota-workers", Count(workers),
                "--refresh-timeout", "20",
            };
            var tempFiles = new List<string>();
            if (targets.Count > 1)
            {
                string emailFile = WriteEmailFile(tempDirectory, "promotion_check_emails_", targets);
                tempFiles.Add(emailFile);
                args.AddRange(new[] { "--email-file", emailFile });
            }
            else
            {
                args.AddRange(new[] { "--email", targets[0] });
            }
            AppendProxyPool(args, proxyPool);
            return new BackendCommandPlan(
                "账号优惠检测(" + Count(targets.Count) + ")",
                args,
                TemporaryFiles: tempFiles);
        }

        public static BackendCommandPlan CreateQuotaUsageProbe(
            string email,
            int refreshTimeoutSeconds,
            IReadOnlyList<string> proxyPool)
        {
            var args = new List<string>
            {
                "--quota-usage",
                "--email", RequireEmail(email),
                "--refresh-timeout", Count(refreshTimeoutSeconds),
            };
            AppendProxyPool(args, proxyPool);
            return new BackendCommandPlan("账号测活", args);
        }

        // ── Deletion ────────────────────────────────────────────────────

        public static BackendCommandPlan CreateDeleteAccount(string email)
        {
            var args = new List<string>
            {
                "--delete-account",
                "--email", RequireEmail(email),
                "--desktop-ipc",
            };
            return new BackendCommandPlan("删除账号", args, TimeoutMilliseconds: 120000);
        }

        /// <summary>
        /// Batch delete with a list of email addresses.
        /// </summary>
        public static BackendCommandPlan CreateBatchDeleteAccounts(
            IReadOnlyList<string> emails,
            int workers = 4,
            string tempDirectory = null)
        {
            List<string> targets = RequireEmails(emails);
            string emailFile = WriteEmailFile(tempDirectory, "delete_emails_", targets);
            var args = new List<string>
            {
                "--delete-account",
                "--email-file", emailFile,
                "--workers", Count(workers),
                "--desktop-ipc",
            };
            return new BackendCommandPlan(
                "批量删除账号 (" + Count(targets.Count) + ")",
                args,
                TemporaryFiles: new[] { emailFile },
                TimeoutMilliseconds: 120000);
        }

        // ── Import ──────────────────────────────────────────────────────

        /// <summary>
        /// Batch import from a list of email addresses. Writes a temp email file.
        /// </summary>
        public static BackendCommandPlan CreateAccountImport(
            string target,
            IReadOnlyList<string> emails,
            int workers = 4,
            int refreshTimeoutSeconds = 60,
            string tempDirectory = null)
        {
            List<string> targets = RequireEmails(emails);
            string normalized = NormalizeImportTarget(target);
            string emailFile = WriteEmailFile(tempDirectory, "oneclick_import_emails_", targets);
            var args = new List<string>
            {
                "--import-cpa",
                "--email-file", emailFile,
                "--workers", Count(workers),
                "--refresh-timeout", Count(refreshTimeoutSeconds),
                "--import-target", normalized,
            };
            return new BackendCommandPlan(
                "一键导入" + ImportTargetLabel(normalized) + " (" + Count(targets.Count) + ")",
                args,
                TemporaryFiles: new[] { emailFile });
        }

        public static string NormalizeImportTarget(string target)
        {
            string value = (target ?? "").Trim().ToLowerInvariant();
            return value is "sub2api" or "cliproxyapi" ? value : "cpa";
        }

        public static string ImportTargetLabel(string target)
        {
            string value = (target ?? "").Trim().ToLowerInvariant();
            if (value == "sub2api") return "SUB2API";
            if (value == "cliproxyapi") return "CLIProxyAPI";
            return "CPA";
        }

        /// <summary>
        /// Single-account import. For a single email, no temp file is needed;
        /// the email is passed directly via --email-file (the backend expects a file).
        /// </summary>
        public static BackendCommandPlan CreateSingleAccountImport(
            string target,
            string email,
            int workers = 4,
            int refreshTimeoutSeconds = 60,
            string tempDirectory = null)
        {
            string normalized = NormalizeImportTarget(target);
            string emailFile = WriteEmailFile(tempDirectory, "single_import_email_", new[] { RequireEmail(email) });
            var args = new List<string>
            {
                "--import-cpa",
                "--email-file", emailFile,
                "--workers", Count(workers),
                "--refresh-timeout", Count(refreshTimeoutSeconds),
                "--import-target", normalized,
            };
            return new BackendCommandPlan(
                "一键导入" + ImportTargetLabel(normalized),
                args,
                TemporaryFiles: new[] { emailFile });
        }

        // ── Export conversion ───────────────────────────────────────────

        public static BackendCommandPlan CreateSessionConversion(
            string sourcePath,
            string format,
            string outputPath)
        {
            string normalized = (format ?? "cpa").Trim().ToLowerInvariant();
            if (normalized.Length == 0) normalized = "cpa";
            var args = new List<string>
            {
                "--convert-session-json", RequireArgument(sourcePath, nameof(sourcePath)),
                "--convert-format", normalized,
                "--convert-output", RequireArgument(outputPath, nameof(outputPath)),
            };
            return new BackendCommandPlan("导出账号转换(" + normalized + ")", args);
        }

        // ── Refresh / maintenance ───────────────────────────────────────

        public static BackendCommandPlan CreateRefreshSession(string email, string sessionFile)
        {
            var args = new List<string> { "--email", RequireEmail(email), "--refresh-session" };
            AppendSessionFile(args, sessionFile);
            return new BackendCommandPlan("刷新Session", args);
        }

        public static BackendCommandPlan CreateRebuildSqlite()
        {
            return new BackendCommandPlan("重建SQLite索引", new List<string> { "--rebuild-sqlite" });
        }

        // ── Inbox ───────────────────────────────────────────────────────

        public static BackendCommandPlan CreateViewInbox(
            string email,
            int limit,
            string mailboxArgument,
            string mailboxLine,
            string sessionFile,
            string mailboxProxy,
            string remailToken,
            string tempDirectory = null)
        {
            var args = new List<string>
            {
                "--desktop-ipc",
                "--view-inbox",
                "--email", RequireEmail(email),
                "--inbox-limit", Count(limit),
            };
            var tempFiles = new List<string>();
            string argument = (mailboxArgument ?? "").Trim();
            string line = (mailboxLine ?? "").Trim();
            if (argument.Length > 0 && line.Length > 0)
            {
                string mailboxFile = Path.Combine(
                    TempDirectory(tempDirectory),
                    "view_inbox_mailbox_" + DateTime.Now.ToString("yyyyMMdd_HHmmss_fff", CultureInfo.InvariantCulture) + ".txt");
                File.WriteAllText(mailboxFile, line + Environment.NewLine, Utf8NoBom);
                tempFiles.Add(mailboxFile);
                args.AddRange(new[] { argument, mailboxFile });
            }
            AppendSessionFile(args, sessionFile);
            string proxy = (mailboxProxy ?? "").Trim();
            if (proxy.Length > 0)
            {
                args.Add("--proxy");
                args.Add(proxy);
            }
            var environment = new Dictionary<string, string>();
            string token = (remailToken ?? "").Trim();
            if (token.Length > 0)
                environment["REMAIL_SERVICE_TOKEN"] = token;
            return new BackendCommandPlan(
                "查看收件箱",
                args,
                EnvironmentVariables: environment,
                TemporaryFiles: tempFiles,
                TimeoutMilliseconds: 120000);
        }

        // ── Shared shape helpers ────────────────────────────────────────

        /// <summary>
        /// Classifies a mailbox pool line into the CLI flag that consumes it.
        /// The legacy parser is the compatibility superset for mixed provider
        /// selections; unknown or comment lines yield an empty flag.
        /// </summary>
        public static string MailboxArgumentForLine(string line)
        {
            string value = (line ?? "").Trim().TrimStart('﻿');
            if (value.Length == 0 || value.StartsWith('#')) return "";
            if (value.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase)
                || value.EndsWith("@edu.liziai.cloud", StringComparison.OrdinalIgnoreCase)
                || value.EndsWith("@liziai.cloud", StringComparison.OrdinalIgnoreCase)) return "--mailbox-file";
            if (value.StartsWith("remail://", StringComparison.OrdinalIgnoreCase)) return "--mailbox-file";
            if (value.StartsWith("smailr://", StringComparison.OrdinalIgnoreCase)) return "--mailbox-file";
            if (value.StartsWith("gmail://", StringComparison.OrdinalIgnoreCase)) return "--mailbox-file";
            if (MailboxCredentialLineParser.TryParseICloudUrlLine(value, out _, out _)) return "--mailbox-file";
            if (value.Contains("----", StringComparison.Ordinal) && value.Split("----", StringSplitOptions.None).Length >= 4) return "--chatai-mailbox-file";
            if (value.Contains("---", StringComparison.Ordinal) && value.Split("---", StringSplitOptions.None).Length >= 3) return "--mailbox-file";
            return "";
        }

        /// <summary>
        /// Appends the registration proxy route: the first pool entry becomes
        /// the scalar --proxy and the full pool is forwarded via --proxy-pool
        /// only when more than one route exists.
        /// </summary>
        public static void AppendProxyPool(List<string> args, IReadOnlyList<string> proxyPool)
        {
            var pool = (proxyPool ?? Array.Empty<string>())
                .Select(item => (item ?? "").Trim())
                .Where(item => item.Length > 0)
                .ToList();
            if (pool.Count == 0) return;
            args.Add("--proxy");
            args.Add(pool[0]);
            if (pool.Count > 1)
            {
                args.Add("--proxy-pool");
                args.Add(string.Join(Environment.NewLine, pool));
            }
        }

        public static void AppendSessionFile(List<string> args, string sessionFile)
        {
            string value = (sessionFile ?? "").Trim();
            if (value.Length == 0) return;
            args.Add("--session-file");
            args.Add(value);
        }

        private static void AppendRegistrationAtOnly(List<string> args)
        {
            args.Add("--registration-at-only");
            AppendNoPhoneReuse(args);
        }

        private static void AppendNoPhoneReuse(List<string> args)
        {
            args.Add("--no-phone-reuse");
        }

        private static void AppendNo2fa(List<string> args, bool disable2fa)
        {
            if (disable2fa) args.Add("--no-2fa");
        }

        private static void AppendCheckPromotion(List<string> args, bool checkPromotion)
        {
            if (checkPromotion) args.Add("--check-promotion-after-registration");
        }

        private static string WriteEmailFile(string tempDirectory, string prefix, IReadOnlyList<string> emails)
        {
            string emailFile = Path.Combine(
                TempDirectory(tempDirectory),
                prefix + DateTime.Now.ToString("yyyyMMdd_HHmmss", CultureInfo.InvariantCulture) + ".txt");
            File.WriteAllLines(emailFile, emails.Select(email => email.Trim()), Utf8NoBom);
            return emailFile;
        }

        private static string TempDirectory(string tempDirectory)
        {
            return string.IsNullOrWhiteSpace(tempDirectory) ? Path.GetTempPath() : tempDirectory;
        }

        private static string RequireArgument(string value, string parameterName)
        {
            string trimmed = (value ?? "").Trim();
            if (trimmed.Length == 0)
                throw new ArgumentException("Backend command argument must not be empty.", parameterName);
            return value;
        }

        private static string RequireEmail(string email)
        {
            string trimmed = (email ?? "").Trim();
            if (trimmed.Length == 0)
                throw new ArgumentException("Backend command requires an account email.", nameof(email));
            return trimmed;
        }

        private static List<string> RequireEmails(IReadOnlyList<string> emails)
        {
            var targets = (emails ?? Array.Empty<string>())
                .Select(email => (email ?? "").Trim())
                .Where(email => email.Length > 0)
                .ToList();
            if (targets.Count == 0)
                throw new ArgumentException("Backend command requires at least one account email.", nameof(emails));
            return targets;
        }

        private static string Count(int value)
        {
            return value.ToString(CultureInfo.InvariantCulture);
        }
    }
}
