using System.Text.Json;

namespace SmsWorkbench
{
    public static class BackendJsonProtocol
    {
        private static readonly string[] LineSeparators = { "\r\n", "\n" };

        public const string Prefix = "@@SMSWORKBENCH_IPC_V1@@";

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
                    && version.GetInt32() == 1
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
            for (int start = value.LastIndexOf('{'); start >= 0; start = value.LastIndexOf('{', start - 1))
            {
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
