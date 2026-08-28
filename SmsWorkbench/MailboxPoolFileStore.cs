using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace SmsWorkbench
{
    internal static class MailboxPoolFileStore
    {
        internal static (int Imported, int Skipped) ImportSupportedLines(string targetPath, IEnumerable<string> sourceLines)
        {
            if (string.IsNullOrWhiteSpace(targetPath)) throw new ArgumentException("Target path is required.", nameof(targetPath));

            string fullPath = Path.GetFullPath(targetPath);
            string? directory = Path.GetDirectoryName(fullPath);
            if (!string.IsNullOrWhiteSpace(directory)) Directory.CreateDirectory(directory);

            var existing = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (File.Exists(fullPath))
            {
                foreach (string line in File.ReadAllLines(fullPath, Encoding.UTF8))
                {
                    string normalized = NormalizeLine(line);
                    if (normalized.Length > 0) existing.Add(normalized);
                }
            }

            int skipped = 0;
            var additions = new List<string>();
            foreach (string raw in sourceLines ?? Enumerable.Empty<string>())
            {
                string line = NormalizeLine(raw);
                if (line.Length == 0 || line.StartsWith('#')) continue;
                if (!IsSupportedImportLine(line) || !existing.Add(line))
                {
                    skipped++;
                    continue;
                }
                additions.Add(line);
            }

            if (additions.Count > 0) File.AppendAllLines(fullPath, additions, new UTF8Encoding(false));
            return (additions.Count, skipped);
        }

        internal static bool TryParseICloudUrlLine(string line, out string email, out string receiveUrl)
        {
            return MailboxCredentialLineParser.TryParseICloudUrlLine(line, out email, out receiveUrl);
        }

        internal static bool IsMailboxPoolLike(string accountType, string mailboxProvider)
        {
            string type = accountType ?? "";
            return type.Contains("邮箱池", StringComparison.OrdinalIgnoreCase)
                || type.Contains("Chatai", StringComparison.OrdinalIgnoreCase)
                || !string.IsNullOrWhiteSpace(mailboxProvider);
        }

        internal static int DeleteMatchingLines(string path, string emailKey, IEnumerable<string> exactLines)
        {
            if (string.IsNullOrWhiteSpace(path) || !File.Exists(path)) return 0;

            var exact = new HashSet<string>(
                (exactLines ?? Enumerable.Empty<string>())
                    .Select(NormalizeLine)
                    .Where(value => value.Length > 0),
                StringComparer.OrdinalIgnoreCase);
            var lines = File.ReadAllLines(path, Encoding.UTF8).ToList();
            int before = lines.Count;
            lines.RemoveAll(line =>
            {
                string value = NormalizeLine(line);
                if (exact.Contains(value)) return true;
                return emailKey.Length > 0 && NormalizeEmailKey(EmailForLine(value)) == emailKey;
            });

            int removed = before - lines.Count;
            if (removed <= 0) return 0;
            WriteAllLinesAtomic(path, lines);
            return removed;
        }

        internal static string NormalizeEmailKey(string email)
        {
            string value = (email ?? "").Trim().TrimStart('\ufeff').ToLowerInvariant();
            if (!value.Contains("@+")) return value;

            string[] parts = value.Split(new[] { "@+" }, StringSplitOptions.None);
            if (parts.Length != 2) return value;
            string[] domains = { "hotmail.com", "outlook.com", "live.com", "msn.com", "gmail.com" };
            foreach (string domain in domains)
            {
                if (parts[1].EndsWith(domain, StringComparison.OrdinalIgnoreCase) && parts[1].Length > domain.Length)
                {
                    string alias = parts[1].Substring(0, parts[1].Length - domain.Length);
                    return parts[0] + "+" + alias + "@" + domain;
                }
            }
            return value;
        }

        internal static string BuildReMailLine(string email, string serviceToken, string orderNo, string purchaseId)
        {
            string normalizedEmail = (email ?? "").Trim();
            string normalizedToken = (serviceToken ?? "").Trim();
            string normalizedOrderNo = (orderNo ?? "").Trim();
            if (normalizedEmail.Length == 0 || normalizedToken.Length == 0 || normalizedOrderNo.Length == 0) return "";

            string line = "remail://" + normalizedEmail + "---" + normalizedToken + "---" + normalizedOrderNo;
            string normalizedPurchaseId = (purchaseId ?? "").Trim();
            return normalizedPurchaseId.Length > 0 ? line + "---" + normalizedPurchaseId : line;
        }

        private static bool IsSupportedImportLine(string line)
        {
            if (TryParseICloudUrlLine(line, out _, out _)) return true;
            string[] parts = line.Split(new[] { "----" }, 4, StringSplitOptions.None);
            return parts.Length >= 4;
        }

        private static string EmailForLine(string line)
        {
            string value = NormalizeLine(line);
            if (value.StartsWith("gmail://", StringComparison.OrdinalIgnoreCase))
                value = value.Substring("gmail://".Length);
            else if (value.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase))
                value = value.Substring("cfworker://".Length);
            else if (value.StartsWith("remail://", StringComparison.OrdinalIgnoreCase))
                value = value.Substring("remail://".Length);
            if (value.Contains("----")) return value.Split(new[] { "----" }, StringSplitOptions.None)[0];
            if (value.Contains("---")) return value.Split(new[] { "---" }, StringSplitOptions.None)[0];
            if (value.Contains('@') && !value.Contains(' ')) return value;
            return "";
        }

        private static string NormalizeLine(string line)
        {
            return (line ?? "").Trim().TrimStart('\ufeff');
        }

        private static void WriteAllLinesAtomic(string path, IReadOnlyCollection<string> lines)
        {
            string directory = Path.GetDirectoryName(Path.GetFullPath(path)) ?? ".";
            string tempPath = Path.Combine(directory, "." + Path.GetFileName(path) + "." + Guid.NewGuid().ToString("N") + ".tmp");
            try
            {
                File.WriteAllLines(tempPath, lines, new UTF8Encoding(false));
                File.Move(tempPath, path, true);
            }
            finally
            {
                if (File.Exists(tempPath)) File.Delete(tempPath);
            }
        }
    }
}
