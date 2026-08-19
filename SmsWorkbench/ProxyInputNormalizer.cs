using System;
using System.Linq;
using System.Text.RegularExpressions;

namespace SmsWorkbench
{
    public static class ProxyInputNormalizer
    {
        private static readonly string[] SupportedSchemes = ["http", "https", "socks5", "socks5h"];
        private static readonly string[] ListSeparators = ["\r\n", "\n", ",", ";"];

        public static string Normalize(string value, string defaultScheme = "http")
        {
            string raw = (value ?? "").Trim().Replace('：', ':');
            if (raw.Length == 0)
                return "";

            string scheme = NormalizeScheme(defaultScheme);
            string remainder = raw;
            int schemeSeparator = raw.IndexOf("://", StringComparison.Ordinal);
            bool hasExplicitScheme = schemeSeparator >= 0;
            if (schemeSeparator >= 0)
            {
                scheme = NormalizeScheme(raw[..schemeSeparator]);
                remainder = raw[(schemeSeparator + 3)..];
            }

            if (remainder.Contains('@', StringComparison.Ordinal))
                return NormalizeUrlForm(scheme, remainder);

            string[] parts = remainder.Split(':');
            if (parts.Length == 4 && int.TryParse(parts[1], out int providerPort))
                return BuildUrl(scheme, parts[0], providerPort, parts[2], parts[3]);
            if (parts.Length == 2 && int.TryParse(parts[1], out int port))
                return BuildUrl(scheme, parts[0], port, "", "");
            if (hasExplicitScheme
                && Uri.TryCreate(scheme + "://" + remainder, UriKind.Absolute, out Uri legacyUri)
                && legacyUri.Host.Length > 0
                && legacyUri.UserInfo.Length == 0
                && legacyUri.AbsolutePath == "/"
                && legacyUri.Query.Length == 0
                && legacyUri.Fragment.Length == 0)
            {
                // Preserve legacy scheme://host placeholders. Runtime validation
                // still rejects them when used as a real upstream proxy.
                return scheme + "://" + remainder;
            }

            throw new FormatException("代理格式应为 host:port、host:port:user:password 或带 http/https/socks5/socks5h 前缀的 URL。");
        }

        public static string[] NormalizeList(string value, string defaultScheme = "http")
            => (value ?? "")
                .Split(ListSeparators, StringSplitOptions.RemoveEmptyEntries)
                .Select(item => item.Trim())
                .Where(item => item.Length > 0)
                .Select(item => Normalize(item, defaultScheme))
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToArray();

        public static string InferCountry(string value)
        {
            string normalized = Normalize(value);
            if (!Uri.TryCreate(normalized, UriKind.Absolute, out Uri uri))
                return "";

            string username = Uri.UnescapeDataString(uri.UserInfo.Split(':', 2)[0]);
            Match match = Regex.Match(username, @"(?:^|[_-])(?:custom[_-]zone|country|region)[_-]([A-Za-z]{2})(?:[_-]|$)", RegexOptions.IgnoreCase);
            if (!match.Success)
                match = Regex.Match(username, @"(?:^|[_-])([A-Za-z]{2})(?:[_-](?:sid|session|\d|$)|$)", RegexOptions.IgnoreCase);
            return match.Success ? match.Groups[1].Value.ToUpperInvariant() : "";
        }

        public static string InferCountryFromPool(string value)
        {
            string[] countries = NormalizeList(value)
                .Select(InferCountry)
                .Where(country => country.Length > 0)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToArray();
            return countries.Length == 1 ? countries[0] : "";
        }

        public static string NormalizeListText(string value, string defaultScheme = "http")
            => string.Join(Environment.NewLine, NormalizeList(value, defaultScheme));

        private static string NormalizeUrlForm(string scheme, string remainder)
        {
            if (!Uri.TryCreate(scheme + "://" + remainder, UriKind.Absolute, out Uri uri) || uri.Port <= 0)
                throw new FormatException("代理 URL 无效或缺少端口。");
            string userInfo = uri.UserInfo;
            if (userInfo.Length == 0)
                return BuildUrl(scheme, uri.Host, uri.Port, "", "");
            string[] credentials = userInfo.Split(':', 2);
            return BuildUrl(
                scheme,
                uri.Host,
                uri.Port,
                Uri.UnescapeDataString(credentials[0]),
                credentials.Length > 1 ? Uri.UnescapeDataString(credentials[1]) : "");
        }

        private static string BuildUrl(string scheme, string host, int port, string username, string password)
        {
            if (string.IsNullOrWhiteSpace(host) || port is < 1 or > 65535)
                throw new FormatException("代理主机或端口无效。");
            string endpoint = host.Contains(':', StringComparison.Ordinal) ? "[" + host.Trim('[', ']') + "]" : host;
            if (username.Length == 0 && password.Length == 0)
                return $"{scheme}://{endpoint}:{port}";
            return $"{scheme}://{Encode(username)}:{Encode(password)}@{endpoint}:{port}";
        }

        private static string NormalizeScheme(string scheme)
        {
            string normalized = (scheme ?? "http").Trim().ToLowerInvariant();
            if (normalized == "socks") normalized = "socks5";
            if (!SupportedSchemes.Contains(normalized, StringComparer.Ordinal))
                throw new FormatException("代理协议仅支持 http、https、socks5 或 socks5h。");
            return normalized;
        }

        private static string Encode(string value) => Uri.EscapeDataString(value ?? "");
    }
}
