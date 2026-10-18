using System.Text;
using System.Text.Json;

namespace SmsWorkbench
{
    /// <summary>
    /// Bounded capture of a backend process's stdout/stderr.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The previous capture kept up to 2,000,000 characters per stream, i.e. up
    /// to ~8 MB of UTF-16 per command, and still had to carry the whole
    /// <c>@@SMSWORKBENCH_V2@@</c> line (623 KB for 434 accounts) as raw text just
    /// so the payload could be parsed after exit. The envelope is parseable the
    /// moment its line arrives, so it is captured structurally and only the
    /// diagnostic tail is retained.
    /// </para>
    /// <para>
    /// The retained tail still has to be large enough for the legacy tail-JSON
    /// fallback (a payload without an envelope) and for error messages; 256 K
    /// characters covers both. Envelope payloads are not limited by it.
    /// </para>
    /// </remarks>
    public sealed class BackendOutputCapture
    {
        /// <summary>Characters kept per stream for diagnostics and the legacy parser.</summary>
        public const int MaxTailChars = 262_144;

        private readonly StringBuilder tail = new();

        /// <summary>The last valid IPC envelope payload, or null when none was seen.</summary>
        public JsonElement? Payload { get; private set; }

        /// <summary>Appends one line to the bounded tail, dropping the oldest characters.</summary>
        public void Append(string line)
        {
            if (line.Length >= MaxTailChars)
            {
                tail.Clear();
                tail.Append(line, line.Length - MaxTailChars, MaxTailChars);
                return;
            }
            tail.AppendLine(line);
            int excess = tail.Length - MaxTailChars;
            if (excess > 0)
                tail.Remove(0, excess);
        }

        /// <summary>
        /// Captures a line that starts with the IPC prefix into <see cref="Payload"/>
        /// and reports whether it was consumable. A prefix line that does not satisfy
        /// the v2 contract returns false and is kept in the tail instead, so the
        /// post-exit parser can still warn about the mismatch.
        /// </summary>
        public bool TryCaptureEnvelope(string line)
        {
            if (!line.StartsWith(BackendJsonProtocol.Prefix, StringComparison.Ordinal))
                return false;
            JsonElement? payload;
            try
            {
                payload = BackendJsonProtocol.ExtractPayload(line);
            }
            catch (JsonException)
            {
                // Malformed envelope: keep the line in the tail so the
                // post-exit parser can report it as a response-parse failure.
                return false;
            }
            if (!payload.HasValue)
                return false;
            Payload = payload;
            return true;
        }

        /// <summary>The retained tail, trimmed.</summary>
        public string Text => tail.ToString().Trim();
    }
}
