// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Moves the proxy routes out of the backend's process command line.
    /// </summary>
    /// <remarks>
    /// The planner emits <c>--proxy &lt;url&gt;</c> / <c>--proxy-pool &lt;urls&gt;</c>
    /// because that is the documented CLI contract and it keeps the plan pure and
    /// testable. But a residential proxy URL is <c>scheme://user:pass@host:port</c>,
    /// and on Windows every same-user process can read another process's command
    /// line (Task Manager's "Command line" column, WMI <c>Win32_Process</c>, ETW).
    /// So the launcher — the single place where argv becomes a process — swaps
    /// those values into environment variables and drops the flags from argv.
    /// The backend accepts both carriers; an explicit flag still wins.
    ///
    /// <para>
    /// <b>Every</b> credential-carrying route is listed in <see cref="Routes"/>,
    /// not just the registration pair. The payment lane builds its batch with
    /// <c>--checkout-proxy-pool</c> / <c>--approve-proxy-pool</c>, whose values are
    /// whole newline-joined pools of <c>scheme://user:pass@host:port</c> entries —
    /// the same secret in a bigger carrier. Leaving them on argv kept the leak the
    /// transport exists to close, and a mixed IPWO pool is also large enough to
    /// approach the 32K Windows command-line limit that the environment carrier
    /// does not have.
    /// </para>
    ///
    /// This is a transport change only: the plan, the UI's redacted argument log
    /// (<see cref="SensitiveDataSanitizer.RedactArguments"/>) and the CLI flags all
    /// keep working as before.
    /// </remarks>
    public static class ProxyArgumentTransport
    {
        public const string ProxyFlag = "--proxy";
        public const string ProxyPoolFlag = "--proxy-pool";
        public const string CheckoutPoolFlag = "--checkout-proxy-pool";
        public const string ApprovePoolFlag = "--approve-proxy-pool";
        public const string ProxyVariable = "SMS_TOOL_PROXY";
        public const string ProxyPoolVariable = "SMS_TOOL_PROXY_POOL";
        public const string CheckoutPoolVariable = "SMS_TOOL_CHECKOUT_PROXY_POOL";
        public const string ApprovePoolVariable = "SMS_TOOL_APPROVE_PROXY_POOL";

        /// <summary>
        /// Every route whose value is a credential, mapped to its transport variable.
        /// </summary>
        /// <remarks>
        /// The single list both <see cref="Extract"/> and its tests read, so adding a
        /// proxy flag to the planner without adding it here is a visible omission
        /// rather than a silent argv leak. <c>--checkout-proxy</c> and its siblings
        /// stay out on purpose: the desktop never emits them (only the pool form), and
        /// a route that is not planned cannot leak.
        /// </remarks>
        public static readonly IReadOnlyDictionary<string, string> Routes =
            new Dictionary<string, string>(StringComparer.Ordinal)
            {
                [ProxyFlag] = ProxyVariable,
                [ProxyPoolFlag] = ProxyPoolVariable,
                [CheckoutPoolFlag] = CheckoutPoolVariable,
                [ApprovePoolFlag] = ApprovePoolVariable,
            };

        /// <summary>Arguments with the proxy flags removed, plus the values to export.</summary>
        public readonly record struct Split(
            IReadOnlyList<string> Arguments,
            IReadOnlyDictionary<string, string> Environment);

        /// <summary>
        /// Removes every route in <see cref="Routes"/> (with its value) from
        /// <paramref name="arguments"/> and returns the values as environment
        /// entries merged over <paramref name="environment"/>.
        /// </summary>
        /// <remarks>
        /// A flag that is not followed by a value — or is followed by another
        /// flag — is dropped on its own and the next token stays in the stream.
        /// Consuming it anyway (argparse's own behaviour) would turn
        /// <c>--proxy --count 3</c> into a proxy literally named <c>--count</c> and
        /// silently delete the count flag. A repeated flag keeps the last value,
        /// matching argparse.
        /// </remarks>
        public static Split Extract(
            IReadOnlyList<string>? arguments,
            IReadOnlyDictionary<string, string>? environment = null)
        {
            var kept = new List<string>();
            var exported = new Dictionary<string, string>(StringComparer.Ordinal);
            if (environment != null)
            {
                foreach (KeyValuePair<string, string> pair in environment)
                    exported[pair.Key] = pair.Value;
            }

            // flag -> last value seen (a repeated flag keeps the last one, like argparse).
            var captured = new Dictionary<string, string>(StringComparer.Ordinal);
            IReadOnlyList<string> source = arguments ?? Array.Empty<string>();
            for (int index = 0; index < source.Count; index++)
            {
                string argument = source[index] ?? "";
                if (!Routes.ContainsKey(argument))
                {
                    kept.Add(argument);
                    continue;
                }

                string? value = index + 1 < source.Count ? source[index + 1] : null;
                if (value == null || value.Length == 0 || value.StartsWith("--", StringComparison.Ordinal))
                {
                    // No value: drop the flag only. The next token is not ours to eat.
                    continue;
                }

                index++; // consume the value slot
                captured[argument] = value;
            }

            foreach (KeyValuePair<string, string> route in Routes)
            {
                if (captured.TryGetValue(route.Key, out string? value) && !string.IsNullOrEmpty(value))
                    exported[route.Value] = value!;
            }
            return new Split(kept, exported);
        }
    }
}
