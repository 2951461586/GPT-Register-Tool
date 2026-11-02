// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// One accounts payload, classified: a full list, an incremental delta with the
    /// complete id set, or a shape the caller must treat as a full list.
    /// </summary>
    public sealed record AccountRefreshPlan(
        IReadOnlyList<JsonElement> Accounts,
        IReadOnlySet<string>? CurrentIds,
        long? Cursor,
        bool IsFullPayload,
        bool ShapeDrift)
    {
        /// <summary>True when the caller can merge instead of rebuilding the pool.</summary>
        public bool IsDelta => CurrentIds != null && !ShapeDrift;
    }

    /// <summary>
    /// Reads an accounts payload into a plan. No window state: the caller decides what
    /// to do with the plan, this decides what the payload *is*.
    /// </summary>
    /// <remarks>
    /// Split from <c>MainWindow.Pools.cs</c> (2026-11-01). The delta shape is easy to
    /// get wrong in ways only a live backend shows -- <c>ids</c> is the complete current
    /// id set (so it, not the delta rows, is what makes a deletion visible), and a
    /// payload that answers in the plain list shape must degrade to a full apply
    /// instead of silently merging nothing.
    /// </remarks>
    public static class AccountRefreshPlanReader
    {
        public static AccountRefreshPlan Read(JsonElement payload)
        {
            bool hasAccounts = payload.TryGetProperty("accounts", out JsonElement accounts)
                && accounts.ValueKind == JsonValueKind.Array;

            long? cursor = null;
            bool isFull = false;
            if (payload.TryGetProperty("cursor", out JsonElement cursorElement)
                && cursorElement.ValueKind == JsonValueKind.Number
                && cursorElement.TryGetInt64(out long value))
            {
                cursor = value;
                isFull = payload.TryGetProperty("full", out JsonElement full)
                    && full.ValueKind == JsonValueKind.True;
            }

            var rows = hasAccounts ? accounts.EnumerateArray().ToList() : new List<JsonElement>();
            IReadOnlySet<string>? ids = ReadIds(payload);
            // A delta without the id set is shape drift: merging it would leave deleted
            // accounts in the grid forever.
            bool drift = hasAccounts && ids == null && payload.TryGetProperty("cursor", out _);
            return new AccountRefreshPlan(rows, ids, cursor, isFull, drift);
        }

        /// <summary>The full current id set from a delta payload, or null when absent.</summary>
        public static IReadOnlySet<string>? ReadIds(JsonElement payload)
        {
            if (!payload.TryGetProperty("ids", out JsonElement idsElement)
                || idsElement.ValueKind != JsonValueKind.Array)
            {
                return null;
            }

            var ids = new HashSet<string>(StringComparer.Ordinal);
            foreach (JsonElement id in idsElement.EnumerateArray())
            {
                ids.Add(id.ValueKind == JsonValueKind.Number
                    ? id.GetInt64().ToString(CultureInfo.InvariantCulture)
                    : (id.GetString() ?? ""));
            }
            return ids;
        }
    }

    /// <summary>The delta cursor and the budget of consecutive incremental refreshes.</summary>
    /// <remarks>
    /// A full payload resets the budget: it is the anti-drift anchor, so a run of deltas
    /// can never hide a divergence for longer than one full refresh. A failed delta
    /// spends the whole budget, so the next refresh is a full one instead of retrying a
    /// channel that just refused.
    /// </remarks>
    public sealed class AccountRefreshCursor
    {
        public long Cursor { get; private set; }

        public int ConsecutiveIncremental { get; private set; }

        public bool CanRefreshIncrementally(int budget) => Cursor > 0 && ConsecutiveIncremental < budget;

        public void Record(AccountRefreshPlan plan)
        {
            if (plan.Cursor is long cursor) Cursor = cursor;
            if (plan.IsFullPayload) ConsecutiveIncremental = 0;
        }

        public void RecordIncremental() => ConsecutiveIncremental++;

        /// <summary>Spend the budget so the next refresh is a full one.</summary>
        public void ForceFull(int budget) => ConsecutiveIncremental = budget;
    }
}
