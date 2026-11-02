// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using System.Text.Json;
using System.Text.Json.Serialization;

namespace SmsWorkbench
{
    /// <summary>
    /// The registration batch payload — `result_schema.json` family
    /// <c>registration.batch</c>, produced by
    /// <c>sms_tool.commands.registration.build_registration_batch_summary</c>.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Typed instead of read key-by-key because <c>JsonElement.TryGetProperty</c>
    /// defaults every absent key to 0: a renamed key reached the operator as
    /// "完成 0/0" with no error anywhere. Every counter is therefore nullable, so
    /// "absent" is distinguishable from a real 0, and
    /// <see cref="MissingCounters"/> turns that difference into one log line.
    /// </para>
    /// <para>
    /// The key set is pinned twice. <c>scripts/result_schema_check.py</c> compares
    /// every <see cref="JsonPropertyNameAttribute"/> below against
    /// <c>result_schema.json</c> (so a property cannot be added or renamed here
    /// without declaring it), and the same manifest is compared against the keys
    /// the Python builder returns (so the declaration cannot drift from the
    /// backend). Both run in the pre-commit hook and in CI.
    /// </para>
    /// <para>
    /// Unknown keys are ignored on purpose: the contract is additive-only, so a
    /// build from an older release must survive a newer backend.
    /// </para>
    /// </remarks>
    public sealed record RegistrationBatchSummary
    {
        private static readonly JsonSerializerOptions Options = new()
        {
            UnmappedMemberHandling = JsonUnmappedMemberHandling.Skip,
            // Exact snake_case matching: a PascalCase key is a different key, and
            // silently accepting it would hide a backend rename.
            PropertyNameCaseInsensitive = false,
        };

        [JsonPropertyName("ok")]
        public bool? Ok { get; init; }

        [JsonPropertyName("batch_id")]
        public string? BatchId { get; init; }

        [JsonPropertyName("total")]
        public int? Total { get; init; }

        [JsonPropertyName("success")]
        public int? Success { get; init; }

        [JsonPropertyName("failed")]
        public int? Failed { get; init; }

        [JsonPropertyName("session_saved")]
        public int? SessionSaved { get; init; }

        [JsonPropertyName("db_saved")]
        public int? DbSaved { get; init; }

        [JsonPropertyName("quality")]
        public JsonElement? Quality { get; init; }

        [JsonPropertyName("promotion")]
        public JsonElement? Promotion { get; init; }

        [JsonPropertyName("funnel")]
        public JsonElement? Funnel { get; init; }

        [JsonPropertyName("health")]
        public JsonElement? Health { get; init; }

        /// <summary>
        /// True for a registration batch, false for the other account-batch
        /// payloads (liveness scan, promotion batch, account health), which also
        /// carry <c>total</c>/<c>success</c>/<c>failed</c> but never
        /// <c>batch_id</c> or <c>funnel</c>.
        /// </summary>
        [JsonIgnore]
        public bool IsRegistrationBatch => BatchId is not null || Funnel.HasValue;

        /// <summary>Counters the label needs; each one is reported when absent.</summary>
        public IEnumerable<string> MissingCounters()
        {
            if (Total is null) yield return "total";
            if (Success is null) yield return "success";
            if (Failed is null) yield return "failed";
        }

        /// <summary>Parse a payload; false when it is not an object or is malformed.</summary>
        public static bool TryParse(JsonElement payload, out RegistrationBatchSummary? summary)
        {
            summary = null;
            if (payload.ValueKind != JsonValueKind.Object) return false;
            try
            {
                summary = JsonSerializer.Deserialize<RegistrationBatchSummary>(payload.GetRawText(), Options);
            }
            catch (JsonException)
            {
                return false;
            }
            return summary is not null;
        }
    }
}
