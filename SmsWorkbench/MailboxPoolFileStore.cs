using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace SmsWorkbench
{
    internal static class MailboxPoolFileStore
    {
        internal static IReadOnlyList<string> DiscoverKnownFiles(string rootDir, string tokenFile, string selectedFile)
        {
            var paths = new List<string>();
            AddExistingTextFile(paths, selectedFile);
            AddExistingTextFile(paths, tokenFile);

            if (!string.IsNullOrWhiteSpace(rootDir) && Directory.Exists(rootDir))
            {
                foreach (string name in new[] { "hotmail.txt", "chatai_mailbox.txt", "chatai.txt" })
                {
                    AddExistingTextFile(paths, Path.Combine(rootDir, name));
                }
                foreach (string path in Directory.GetFiles(rootDir, "*chatai*.txt", SearchOption.TopDirectoryOnly))
                {
                    AddExistingTextFile(paths, path);
                }
            }

            return paths;
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

        private static void AddExistingTextFile(List<string> paths, string path)
        {
            if (string.IsNullOrWhiteSpace(path) || !File.Exists(path)) return;
            if (!path.EndsWith(".txt", StringComparison.OrdinalIgnoreCase)) return;
            string fullPath = Path.GetFullPath(path);
            if (!paths.Contains(fullPath, StringComparer.OrdinalIgnoreCase)) paths.Add(fullPath);
        }

        private static string EmailForLine(string line)
        {
            string value = NormalizeLine(line);
            if (value.StartsWith("gmail://", StringComparison.OrdinalIgnoreCase))
                value = value.Substring("gmail://".Length);
            else if (value.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase))
                value = value.Substring("cfworker://".Length);
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
