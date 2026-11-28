namespace SmsWorkbench.Services
{
    /// <summary>
    /// Window-independent resolver for workbench settings, proxy routes, data paths,
    /// URL validation, and lightweight display formatting extracted from MainWindow.Helpers.
    /// </summary>
    public static class WorkbenchSettingsResolver
    {
        public static string GetRegistrationProxy(ISettingsService settings)
        {
            return BackendJson.FirstNonEmpty(
                settings.GetString("proxy.registration"),
                settings.GetString("registration_proxy"),
                settings.GetString("proxy.default"));
        }

        public static List<string> GetRegistrationProxyPool(ISettingsService settings)
        {
            var values = new List<string>();
            string primary = GetRegistrationProxy(settings);
            if (primary.Length > 0)
            {
                values.Add(primary);
            }
            values.AddRange(settings.GetStringList("proxy.pool"));
            return values
                .Select(item => item.Trim())
                .Where(item => item.Length > 0)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToList();
        }

        public static string GetMailboxProxy(ISettingsService settings)
        {
            return BackendJson.FirstNonEmpty(
                settings.GetString("mailbox_proxy"),
                settings.GetString("email_registration.mailbox_proxy"),
                settings.GetString("proxy.mailbox"));
        }

        public static string GetConfiguredCfWorkerDomain(ISettingsService settings)
        {
            string domain = settings.GetString("email_registration.cfworker_domain").Trim().TrimStart('@');
            if (domain.Length > 0)
            {
                return domain;
            }
            domain = settings.GetString("email_registration.cfworker.domain").Trim().TrimStart('@');
            return domain.Length > 0 ? domain : "liziai.cloud";
        }

        public static string GetConfiguredSmailrDomain(ISettingsService settings)
        {
            string domain = settings.GetString("email_registration.smailr.default_domain").Trim().TrimStart('@');
            if (domain.Length > 0)
            {
                return domain;
            }
            domain = settings.GetString("email_registration.smailr.domain").Trim().TrimStart('@');
            return domain.Length > 0 ? domain : "smailr.com";
        }

        public static string GetDatabasePath(string rootDir, ISettingsService settings)
        {
            string configured = settings.GetString("storage.sqlite_path");
            if (configured.Length == 0)
            {
                return Path.Combine(rootDir, "runtime", "accounts.sqlite3");
            }
            string expanded = Environment.ExpandEnvironmentVariables(configured);
            return Path.IsPathRooted(expanded) ? expanded : Path.Combine(rootDir, expanded);
        }

        public static string GetMailboxTokenFile(string rootDir, string configDir, ISettingsService settings)
        {
            string configured = settings.GetString("email_registration.token_file");
            string expanded = configured.Length > 0 ? Environment.ExpandEnvironmentVariables(configured) : "mailbox_tokens.txt";
            if (Path.IsPathRooted(expanded))
            {
                return expanded;
            }
            string inConfigDir = Path.Combine(configDir, expanded);
            string legacy = Path.Combine(rootDir, expanded);
            return !File.Exists(inConfigDir) && File.Exists(legacy) ? legacy : inConfigDir;
        }

        public static bool ShouldOpenWithNotepad(string? path)
        {
            string extension = Path.GetExtension(path ?? string.Empty).ToLowerInvariant();
            return extension == ".json" || extension == ".txt" || extension == ".log";
        }

        public static bool IsHttpUrl(string? url)
        {
            return Uri.TryCreate(url, UriKind.Absolute, out Uri? uri)
                && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps);
        }

        public static string OnlyDigits(string? raw)
        {
            string digits = new string((raw ?? string.Empty).Where(char.IsDigit).ToArray());
            return digits.Length == 0 ? "0" : digits;
        }

        public static string Mask(string? value)
        {
            string trimmed = (value ?? string.Empty).Trim();
            return trimmed.Length <= 12
                ? trimmed
                : trimmed.Substring(0, 6) + "..." + trimmed.Substring(trimmed.Length - 4);
        }
    }
}
