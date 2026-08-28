using System.Text.Json;

namespace SmsWorkbench.WebHost;

public sealed record CommandDefaults(
    string PythonExecutable,
    IReadOnlyList<string> ProxyPool,
    string CfWorkerDomain,
    string SmailrDomain);

public sealed class ServerCommandDefaults
{
    private readonly RepositoryPaths _paths;

    public ServerCommandDefaults(RepositoryPaths paths) => _paths = paths;

    public CommandDefaults Load()
    {
        if (!File.Exists(_paths.ConfigPath))
            return new CommandDefaults("python", Array.Empty<string>(), "", "");
        using JsonDocument document = JsonDocument.Parse(File.ReadAllText(_paths.ConfigPath));
        JsonElement root = document.RootElement;
        var proxies = new List<string>();
        string primary = Text(root, "proxy", "registration");
        if (primary.Length == 0) primary = Text(root, "registration_proxy");
        if (primary.Length > 0) proxies.Add(primary);
        if (TryPath(root, out JsonElement pool, "proxy", "pool") && pool.ValueKind == JsonValueKind.Array)
        {
            foreach (JsonElement item in pool.EnumerateArray())
            {
                string value = item.ValueKind == JsonValueKind.String ? item.GetString()?.Trim() ?? "" : "";
                if (value.Length > 0 && !proxies.Contains(value, StringComparer.OrdinalIgnoreCase))
                    proxies.Add(value);
            }
        }
        return new CommandDefaults(
            First(Text(root, "runtime", "python_path"), "python"),
            proxies,
            Text(root, "email_registration", "cfworker_domain"),
            First(Text(root, "email_registration", "smailr", "default_domain"), "smailr.com"));
    }

    private static string Text(JsonElement root, params string[] path)
        => TryPath(root, out JsonElement value, path) && value.ValueKind == JsonValueKind.String
            ? value.GetString()?.Trim() ?? ""
            : "";

    private static bool TryPath(JsonElement root, out JsonElement value, params string[] path)
    {
        value = root;
        foreach (string name in path)
        {
            if (value.ValueKind != JsonValueKind.Object || !value.TryGetProperty(name, out value))
                return false;
        }
        return true;
    }

    private static string First(params string[] values)
        => values.FirstOrDefault(value => !string.IsNullOrWhiteSpace(value)) ?? "";
}
