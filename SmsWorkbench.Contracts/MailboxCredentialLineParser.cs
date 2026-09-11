namespace SmsWorkbench;

using System;
using System.Linq;
using System.Text.RegularExpressions;

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

    /// <summary>
    /// 解析导出/导入用的邮箱凭据行（window-independent，placement rule 8）。
    /// 支持 4 段 "----"（email/password/clientId/refreshToken，其中 clientId 与
    /// refreshToken 按 GUID 形状自动纠位）与 3 段 "---"（缺 clientId，回退到
    /// <paramref name="fallbackClientIdFactory"/>）。返回 false 表示该行不是
    /// 可导入的凭据行（注释、cfworker://、liziai.cloud 行等）。
    /// </summary>
    internal static bool TryParseMailboxExportParts(
        string source,
        Func<string>? fallbackClientIdFactory,
        out string email,
        out string password,
        out string clientId,
        out string refreshToken)
    {
        email = "";
        password = "";
        clientId = "";
        refreshToken = "";

        string value = (source ?? "").Trim().TrimStart('\ufeff');
        if (value.Length == 0 || value.StartsWith("#")) return false;
        if (value.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase)
            || value.EndsWith("@edu.liziai.cloud", StringComparison.OrdinalIgnoreCase)
            || value.EndsWith("@liziai.cloud", StringComparison.OrdinalIgnoreCase))
        {
            return false;
        }

        if (value.Contains("----"))
        {
            string[] parts = value.Split(new[] { "----" }, StringSplitOptions.None);
            if (parts.Length < 4) return false;
            email = parts[0].Trim();
            password = parts[1].Trim();
            string p2 = parts[2].Trim();
            string p3 = string.Join("----", parts.Skip(3)).Trim();
            clientId = LooksMicrosoftClientId(p2) || !LooksMicrosoftClientId(p3) ? p2 : p3;
            refreshToken = LooksMicrosoftClientId(p2) || !LooksMicrosoftClientId(p3) ? p3 : p2;
            return true;
        }

        if (value.Contains("---"))
        {
            string[] parts = value.Split(new[] { "---" }, StringSplitOptions.None);
            if (parts.Length < 3) return false;
            email = parts[0].Trim();
            password = parts[1].Trim();
            clientId = fallbackClientIdFactory?.Invoke() ?? "";
            refreshToken = parts[2].Trim();
            return true;
        }

        return false;
    }

    internal static bool LooksMicrosoftClientId(string value)
    {
        return Regex.IsMatch((value ?? "").Trim(), "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$");
    }
}
