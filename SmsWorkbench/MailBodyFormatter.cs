using System;
using System.Net;
using System.Text.RegularExpressions;

namespace SmsWorkbench
{
    public static class MailBodyFormatter
    {
        public static string ToDisplayText(string body, string preview = "")
        {
            string value = string.IsNullOrWhiteSpace(body) ? preview ?? "" : body;
            if (string.IsNullOrWhiteSpace(value)) return "";

            value = Regex.Replace(value, @"(?is)<(script|style|head)\b[^>]*>.*?</\1\s*>", " ");
            value = Regex.Replace(value, @"(?is)<br\s*/?>", "\n");
            value = Regex.Replace(value, @"(?is)</(p|div|section|article|h[1-6]|tr|table|ul|ol)\s*>", "\n");
            value = Regex.Replace(value, @"(?is)<li\b[^>]*>", "\n- ");
            value = Regex.Replace(value, @"(?is)<[^>]+>", " ");
            value = WebUtility.HtmlDecode(value)
                .Replace("\u00a0", " ", StringComparison.Ordinal)
                .Replace("\u200b", "", StringComparison.Ordinal)
                .Replace("\r\n", "\n", StringComparison.Ordinal)
                .Replace('\r', '\n');

            string[] lines = value.Split('\n');
            for (int index = 0; index < lines.Length; index++)
            {
                lines[index] = Regex.Replace(lines[index], @"[\t ]+", " ").Trim();
                lines[index] = Regex.Replace(lines[index], @"\s+([,.;:!?，。；：！？])", "$1");
            }
            value = string.Join("\n", lines);
            value = Regex.Replace(value, @"\n{3,}", "\n\n");
            return value.Trim();
        }
    }
}
