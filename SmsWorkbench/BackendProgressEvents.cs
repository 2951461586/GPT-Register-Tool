using System.Text.Json;

namespace SmsWorkbench
{
    public sealed record BackendProgressEvent(
        string Domain,
        string RunId,
        string AccountRef,
        string Method,
        string Stage,
        string Status,
        string Detail,
        int Attempt = 0,
        int MaxAttempts = 0,
        string Country = "");

    public static class BackendProgressEventParser
    {
        public const string Prefix = "@@SMSWORKBENCH_EVENT_V1@@";

        public static bool TryParse(string line, out BackendProgressEvent value)
        {
            value = null;
            string text = line ?? "";
            if (!text.StartsWith(Prefix, StringComparison.Ordinal))
                return false;
            try
            {
                using JsonDocument document = JsonDocument.Parse(text[Prefix.Length..]);
                JsonElement root = document.RootElement;
                if (root.GetProperty("version").GetInt32() != 1
                    || !string.Equals(Text(root, "type"), "event", StringComparison.Ordinal)
                    || !root.TryGetProperty("payload", out JsonElement payload)
                    || payload.ValueKind != JsonValueKind.Object)
                    return false;
                string stage = Text(payload, "stage");
                if (stage.Length == 0)
                    return false;
                value = new BackendProgressEvent(
                    Text(payload, "domain"),
                    Text(payload, "run_id"),
                    Text(payload, "account_ref"),
                    Text(payload, "method"),
                    stage,
                    First(Text(payload, "status"), Text(payload, "state"), "running"),
                    First(Text(payload, "detail"), Text(payload, "message")),
                    Number(payload, "attempt"),
                    Number(payload, "max_attempts"),
                    Text(payload, "country"));
                return true;
            }
            catch (JsonException)
            {
                return false;
            }
            catch (InvalidOperationException)
            {
                return false;
            }
            catch (KeyNotFoundException)
            {
                return false;
            }
        }

        private static string Text(JsonElement element, string name)
            => element.TryGetProperty(name, out JsonElement value) && value.ValueKind == JsonValueKind.String
                ? value.GetString() ?? ""
                : "";

        private static int Number(JsonElement element, string name)
            => element.TryGetProperty(name, out JsonElement value) && value.TryGetInt32(out int number)
                ? number
                : 0;

        private static string First(params string[] values)
            => values.FirstOrDefault(value => !string.IsNullOrWhiteSpace(value))?.Trim() ?? "";
    }
}
