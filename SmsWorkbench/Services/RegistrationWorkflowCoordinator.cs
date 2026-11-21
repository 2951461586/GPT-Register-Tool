using SmsWorkbench.Localization;

namespace SmsWorkbench.Services
{
    /// <summary>
    /// Window-independent coordinator for registration source planning, candidate
    /// row deduplication, and mailbox-line resolution across the resident backend
    /// and local pool files.
    /// </summary>
    public sealed class RegistrationWorkflowCoordinator
    {
        public static (int Imported, int Skipped) ImportMailboxFile(string sourcePath, string targetFile)
        {
            string[] lines = File.ReadAllLines(sourcePath, Encoding.UTF8);
            return MailboxPoolFileStore.ImportSupportedLines(targetFile, lines);
        }

        public static List<PoolRow> SelectDistinctAccountRows(
            IEnumerable<PoolRow> selectedOrCurrent,
            PoolViewModel? fallbackPool = null)
        {
            var rows = selectedOrCurrent
                .Where(r => r != null && !string.IsNullOrWhiteSpace(r.Identifier))
                .ToList();
            if (rows.Count == 0 && fallbackPool != null)
            {
                rows = fallbackPool.AllRows
                    .Where(fallbackPool.Matches)
                    .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                    .ToList();
            }
            return rows
                .GroupBy(r => r.Identifier.Trim(), StringComparer.OrdinalIgnoreCase)
                .Select(g => g.First())
                .ToList();
        }

        public static BackendCommandPlan? TryBuildSourcePlan(
            RegisterOptions options,
            IReadOnlyList<string> registrationProxyPool,
            Func<string> getCfWorkerDomain,
            Func<string> getSmailrDomain,
            Func<string> getChataiMailboxFilePath,
            Action<string> log,
            out string? errorTitle,
            out string? errorMessage)
        {
            errorTitle = null;
            errorMessage = null;

            if (options.Source == "phone")
            {
                return BackendCommandPlanner.CreatePhoneRegistration(
                    options.Count,
                    registrationProxyPool,
                    disable2fa: options.Disable2fa,
                    checkPromotion: options.CheckPromotion);
            }

            if (options.Source == "cfworker")
            {
                return BackendCommandPlanner.CreateCfWorkerRegistration(
                    getCfWorkerDomain(),
                    options.Count,
                    options.Workers,
                    registrationProxyPool,
                    disable2fa: options.Disable2fa,
                    checkPromotion: options.CheckPromotion);
            }

            if (options.Source == "remail_target")
            {
                return BackendCommandPlanner.CreateRemailTargetRegistration(
                    options.Count,
                    options.Workers,
                    registrationProxyPool,
                    disable2fa: options.Disable2fa,
                    checkPromotion: options.CheckPromotion);
            }

            if (options.Source == "smailr")
            {
                return BackendCommandPlanner.CreateSmailrRegistration(
                    getSmailrDomain(),
                    options.Count,
                    options.Workers,
                    registrationProxyPool,
                    disable2fa: options.Disable2fa,
                    checkPromotion: options.CheckPromotion);
            }

            if (options.Source != "pool")
            {
                log("未知注册方式：" + SensitiveDataSanitizer.Redact(options.Source));
                errorTitle = Loc.T("不支持的注册方式");
                errorMessage = Loc.F("注册方式“{0}”没有对应的执行路径。", options.Source);
                return null;
            }

            string mailboxFile = getChataiMailboxFilePath();
            if (string.IsNullOrWhiteSpace(mailboxFile) || !File.Exists(mailboxFile))
            {
                errorTitle = Loc.T("缺少邮箱文件");
                errorMessage = Loc.T("未选择邮箱，且未找到 Chatai 邮箱文件。请先导入邮箱，或勾选要注册的邮箱记录。");
                return null;
            }

            return BackendCommandPlanner.CreateMailboxFileRegistration(
                Loc.T("一键注册"),
                BackendCommandPlanner.ChataiMailboxFileFlag,
                mailboxFile,
                options.Count,
                options.Workers,
                registrationAtOnly: true,
                registrationProxyPool,
                disable2fa: options.Disable2fa,
                checkPromotion: options.CheckPromotion);
        }

        public async Task<string> FindMailboxLineForRowAsync(
            IDesktopReadClient desktopRead,
            PoolRow? row,
            string chataiPath,
            string tokenFilePath,
            Func<string, string> onlyDigits,
            Action<string> log,
            CancellationToken ct = default)
        {
            if (row == null) return "";
            if (!string.IsNullOrWhiteSpace(row.MailboxLine)) return row.MailboxLine.Trim();

            string fromDb = await FindMailboxLineFromBackendAsync(desktopRead, row, onlyDigits, log, ct).ConfigureAwait(true);
            if (fromDb.Length > 0) return fromDb;

            string email = (row.Identifier ?? "").Trim();
            if (email.Length == 0) return "";
            var candidateEmails = new List<string> { email };

            var paths = new List<string> { row.SourcePath, chataiPath, tokenFilePath };
            foreach (string path in paths.Where(p => !string.IsNullOrWhiteSpace(p)).Distinct(StringComparer.OrdinalIgnoreCase))
            {
                if (!File.Exists(path) || !path.EndsWith(".txt", StringComparison.OrdinalIgnoreCase)) continue;
                foreach (string raw in File.ReadAllLines(path, Encoding.UTF8))
                {
                    string value = raw.Trim().TrimStart('\ufeff');
                    bool matched = candidateEmails.Any(candidate =>
                        value.StartsWith("gmail://" + candidate, StringComparison.OrdinalIgnoreCase)
                        || value.StartsWith(candidate + "----", StringComparison.OrdinalIgnoreCase)
                        || value.StartsWith(candidate + "---", StringComparison.OrdinalIgnoreCase));
                    if (matched && BackendCommandPlanner.MailboxArgumentForLine(value).Length > 0)
                    {
                        return value;
                    }
                }
            }
            return "";
        }

        public async Task<string> FindMailboxLineFromBackendAsync(
            IDesktopReadClient desktopRead,
            PoolRow row,
            Func<string, string> onlyDigits,
            Action<string> log,
            CancellationToken ct = default)
        {
            if (row == null) return "";
            try
            {
                string line = await desktopRead
                    .ReadMailboxLineAsync(onlyDigits(row.RawLine), row.Identifier, ct)
                    .ConfigureAwait(true);
                return (line ?? "").Trim();
            }
            catch (Exception ex)
            {
                log("读取邮箱 backend 失败：" + SensitiveDataSanitizer.Redact(ex.Message));
            }
            return "";
        }
    }
}
