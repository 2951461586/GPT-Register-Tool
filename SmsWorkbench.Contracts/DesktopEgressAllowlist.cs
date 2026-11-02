// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// The only hosts the desktop process may talk to directly.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The desktop is a <b>shell</b>: provider and protocol behaviour belongs to
    /// <c>sms_tool</c> (docs/architecture.md, Rule 03/05/13). Exactly two surfaces
    /// need their own socket anyway -- the one-click SMS dialog reads the vendor
    /// catalog and balance for the operator's *interactive* choice, and the update
    /// check asks GitHub for the latest release -- and those are the two that used
    /// to call <c>new HttpClient()</c> with no proxy, no logging and hosts scattered
    /// across two files (2026-11-01 P7).
    /// </para>
    /// <para>
    /// The allowlist is deliberately a literal set here (Contracts cannot depend on
    /// <c>Models/SmsProviderCatalog</c> without inverting the dependency direction).
    /// <c>tests/workbench/test_desktop_egress_allowlist.py</c> asserts it covers every
    /// host the two callers actually use and that nothing else opens a socket, so a
    /// new vendor cannot be added by adding an endpoint in one file.
    /// </para>
    /// </remarks>
    public static class DesktopEgressAllowlist
    {
        /// <summary>Vendor catalog hosts plus the release feed. HTTPS only.</summary>
        public static readonly IReadOnlySet<string> Hosts = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            // The vendor catalog and balance arrive over the backend channel now
            // (P7-B), so the release feed is the only direct egress left.
            "api.github.com",
        };
        /// <summary>True for an allow-listed HTTPS URL. Anything else must not leave the process.</summary>
        public static bool IsAllowed(Uri? uri)
        {
            return uri != null
                && uri.IsAbsoluteUri
                && string.Equals(uri.Scheme, Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase)
                && Hosts.Contains(uri.Host);
        }
    }
}
