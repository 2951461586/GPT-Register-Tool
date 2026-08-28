namespace SmsWorkbench;

internal static class MailboxCredentialLineParser
{
    internal static bool TryParseICloudUrlLine(string line, out string email, out string receiveUrl)
    {
        email = "";
        receiveUrl = "";
        string value = (line ?? "").Trim().TrimStart('﻿');
        foreach (string delimiter in new[] { "----", "---" })
        {
            int separator = value.IndexOf(delimiter, StringComparison.Ordinal);
            if (separator <= 0) continue;

            string candidateEmail = value[..separator].Trim().ToLowerInvariant();
            string candidateUrl = value[(separator + delimiter.Length)..].Trim();
            int at = candidateEmail.LastIndexOf('@');
            if (at <= 0 || at == candidateEmail.Length - 1 || candidateEmail.Contains(' ')) continue;
            string domain = candidateEmail[(at + 1)..];
            if (!domain.Equals("icloud.com", StringComparison.OrdinalIgnoreCase)
                && !domain.Equals("me.com", StringComparison.OrdinalIgnoreCase)
                && !domain.Equals("mac.com", StringComparison.OrdinalIgnoreCase)) continue;
            if (!Uri.TryCreate(candidateUrl, UriKind.Absolute, out Uri? uri)
                || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps)) continue;

            email = candidateEmail;
            receiveUrl = candidateUrl;
            return true;
        }
        return false;
    }
}
