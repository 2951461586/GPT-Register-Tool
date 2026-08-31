using System.Text.Json;

namespace SmsWorkbench
{
    public static class BackendJsonProtocol
    {
        private static readonly string[] LineSeparators = { "\r\n", "\n" };

        public const string Prefix = "@@SMSWORKBENCH_V2@@";

        public static JsonElement? ExtractPayload(string standardOutput, Action<string>? onWarning = null)
        {
            string[] lines = (standardOutput ?? "").Split(LineSeparators, StringSplitOptions.None);
            bool sawEnvelope = false;
            bool sawEnvelopeMismatch = false;
            for (int index = lines.Length - 1; index >= 0; index--)
            {
                string line = lines[index].Trim();
                if (!line.StartsWith(Prefix, StringComparison.Ordinal)) continue;
                sawEnvelope = true;
                string envelopeJson = line.Substring(Prefix.Length);
                using JsonDocument envelope = JsonDocument.Parse(envelopeJson);
                JsonElement root = envelope.RootElement;
                // A present envelope whose version/schema does not satisfy the
                // v2 result contract is a protocol/version mismatch, NOT a
                // legacy payload. Track it so we can warn instead of silently
                // downgrading to the legacy tail-JSON parser below.
                bool versionOk = root.TryGetProperty("version", out JsonElement version)
                    && version.ValueKind == JsonValueKind.Number
                    && version.TryGetInt32(out int versionNumber)
                    && versionNumber == 2;
                bool schemaOk = root.TryGetProperty("schema", out JsonElement schema)
                    && string.Equals(schema.GetString(), "smsworkbench.ipc.v2", StringComparison.Ordinal);
                if (!versionOk || !schemaOk)
                {
                    sawEnvelopeMismatch = true;
                    continue;
                }
                if (root.TryGetProperty("type", out JsonElement type)
                    && string.Equals(type.GetString(), "result", StringComparison.Ordinal)
                    && root.TryGetProperty("payload", out JsonElement payload))
                {
                    return payload.Clone();
                }
            }

            if (sawEnvelope)
            {
                // An envelope was present but it did not satisfy the v2 result
                // contract. Refuse to silently fall back to the legacy parser —
                // that would misattribute a protocol/version mismatch to a
                // legacy payload and hide the real cause. Warn and return no
                // payload so the caller sees an explicit missing-result state.
                if (sawEnvelopeMismatch)
                    onWarning?.Invoke(
                        "Backend IPC envelope present but version/schema did not match smsworkbench.ipc.v2; not falling back to legacy parser");
                return null;
            }
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
