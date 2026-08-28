using System.Text.Json;

namespace SmsWorkbench
{
    public static class BackendJsonProtocol
    {
        private static readonly string[] LineSeparators = { "\r\n", "\n" };

        public const string Prefix = "@@SMSWORKBENCH_V2@@";

        public static JsonElement? ExtractPayload(string standardOutput)
        {
            string[] lines = (standardOutput ?? "").Split(LineSeparators, StringSplitOptions.None);
            bool sawEnvelope = false;
            for (int index = lines.Length - 1; index >= 0; index--)
            {
                string line = lines[index].Trim();
                if (!line.StartsWith(Prefix, StringComparison.Ordinal)) continue;
                sawEnvelope = true;
                string envelopeJson = line.Substring(Prefix.Length);
                using JsonDocument envelope = JsonDocument.Parse(envelopeJson);
                JsonElement root = envelope.RootElement;
                if (root.TryGetProperty("version", out JsonElement version)
                    && version.GetInt32() == 2
                    && root.TryGetProperty("schema", out JsonElement schema)
                    && string.Equals(schema.GetString(), "smsworkbench.ipc.v2", StringComparison.Ordinal)
                    && root.TryGetProperty("type", out JsonElement type)
                    && string.Equals(type.GetString(), "result", StringComparison.Ordinal)
                    && root.TryGetProperty("payload", out JsonElement payload))
                {
                    return payload.Clone();
                }
            }

            if (sawEnvelope) return null;
            return ExtractLegacyPayload(standardOutput);
        }

        private static JsonElement? ExtractLegacyPayload(string standardOutput)
        {
            string value = (standardOutput ?? "").Trim();
            // The trailing JSON (if any) parses on the first attempt; the loop
            // only keeps scanning when the tail holds no complete object, so
            // bound the attempts to avoid quadratic parsing on brace-heavy logs.
            const int maxAttempts = 200;
            int attempts = 0;
            for (int start = value.LastIndexOf('{'); start >= 0; start = value.LastIndexOf('{', start - 1))
            {
                if (++attempts > maxAttempts) break;
                try
                {
                    using JsonDocument document = JsonDocument.Parse(value.Substring(start));
                    return document.RootElement.Clone();
                }
                catch (JsonException)
                {
                }
            }
            return null;
        }
    }
}
