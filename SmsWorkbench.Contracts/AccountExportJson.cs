#nullable enable

namespace SmsWorkbench;

using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.Json;

/// <summary>
/// Window-independent shaping of one account's exported session JSON
/// (placement rule 8: parsing/shaping stays out of MainWindow and is xunit-testable).
/// </summary>
internal static class AccountExportJson
{
    /// <summary>Deep copy of nested dictionaries/lists so edits never touch the source record.</summary>
    internal static object Clone(object value)
    {
        if (value is Dictionary<string, object> map)
        {
            var clean = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            foreach (var pair in map)
            {
                clean[pair.Key] = Clone(pair.Value);
            }
            return clean;
        }
        if (value is List<object> list)
        {
            return list.Select(Clone).ToList();
        }
        return value;
    }

    internal static void EnsureEmail(Dictionary<string, object> item, string? identifier)
    {
        string email = (identifier ?? "").Trim();
        if (email.Length == 0) return;
        if (!BackendJson.TryGetMap(item, "user", out var user))
        {
            user = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            item["user"] = user;
        }
        if (BackendJson.GetString(user, "email").Length == 0)
        {
            user["email"] = email;
        }
    }

    internal static void EnsureRefreshToken(Dictionary<string, object> item, Dictionary<string, object> sourceData)
    {
        string rt = FirstString(
            BackendJson.GetString(sourceData, "oauth_refresh_token"),
            BackendJson.GetString(sourceData, "refresh_token"),
            BackendJson.NestedString(sourceData, "codex_session", "refresh_token"),
            BackendJson.NestedString(sourceData, "token", "refresh_token"),
            BackendJson.NestedString(sourceData, "credentials", "refresh_token")
        );
        item["refresh_token"] = rt;
        if (BackendJson.GetString(item, "oauth_refresh_token").Length == 0 && rt.Length > 0)
        {
            item["oauth_refresh_token"] = rt;
        }
    }

    internal static string FirstString(params string?[] values)
    {
        foreach (string? value in values)
        {
            string text = (value ?? "").Trim();
            if (text.Length > 0) return text;
        }
        return "";
    }

    internal static string DedupKey(Dictionary<string, object> item, string? identifier)
    {
        if (BackendJson.TryGetMap(item, "user", out var user))
        {
            string userEmail = BackendJson.GetString(user, "email").Trim();
            if (userEmail.Length > 0) return userEmail.ToLowerInvariant();
        }
        string email = BackendJson.GetString(item, "email").Trim();
        if (email.Length > 0) return email.ToLowerInvariant();
        email = (identifier ?? "").Trim();
        if (email.Length > 0) return email.ToLowerInvariant();
        return JsonSerializer.Serialize(item);
    }
}
