// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using System.Net;

namespace SmsWorkbench
{
    /// <summary>
    /// The desktop process's one direct-HTTP entry point.
    /// </summary>
    /// <remarks>
    /// Everything that leaves this process outside the Python backend goes through
    /// the client this factory returns: the one-click SMS catalog/balance reads and
    /// the update check. That gives the three properties the previous two
    /// <c>new HttpClient()</c> call sites did not have -- one host allowlist
    /// (<see cref="DesktopEgressAllowlist"/>), one proxy decision, and one log line
    /// per request -- without asking the vendor clients to change their shape.
    /// </remarks>
    public interface IDesktopEgress
    {
        /// <summary>A shared, allowlist-guarded client. Owned by the egress; do not dispose.</summary>
        HttpClient CreateClient();

        bool IsAllowed(Uri? uri);
    }

    public sealed class DesktopEgress : IDesktopEgress, IDisposable
    {
        private readonly HttpClient client;
        private readonly bool viaProxy;

        public DesktopEgress(ISettingsService settings, Serilog.ILogger logger)
        {
            // Vendor reads and the release check follow the mailbox lane: that is the
            // egress purpose they belong to (an operator who isolates mailbox traffic
            // expects the vendor API to leave the same way). No new config key: the
            // lane already exists, and a second knob would only invite the two to
            // disagree.
            string proxy = ResolveMailboxLaneProxy(settings, logger);
            viaProxy = proxy.Length > 0;

            var transport = new SocketsHttpHandler
            {
                // Recycle connections: a long-lived client plus DNS changes is how a
                // desktop app ends up pinned to a dead proxy endpoint.
                PooledConnectionLifetime = TimeSpan.FromMinutes(5),
                Proxy = viaProxy ? new WebProxy(proxy) : null,
                UseProxy = viaProxy,
            };
            // The guard sits in front of the transport, so every request is checked
            // and logged in exactly one place.
            var guard = new EgressGuard(logger, viaProxy) { InnerHandler = transport };
            client = new HttpClient(guard) { Timeout = TimeSpan.FromSeconds(60) };
        }

        public HttpClient CreateClient() => client;

        public bool IsAllowed(Uri? uri) => DesktopEgressAllowlist.IsAllowed(uri);

        public void Dispose() => client.Dispose();

        /// <summary>
        /// The mailbox lane's first proxy as an endpoint <see cref="WebProxy"/> can
        /// parse, or "" when the lane declares nothing usable.
        /// </summary>
        /// <remarks>
        /// <para>
        /// The canonical lane shape is an <b>object</b> — `proxy.lanes.mailbox.pool`
        /// (written by <see cref="SettingsService"/>'s own save path). Reading the
        /// object itself is what shipped on 2026-11-02: `GetStringList` stringifies
        /// a <c>JsonObject</c> to its JSON text, `new WebProxy("{...}")` threw
        /// <see cref="UriFormatException"/> in the constructor, and the window never
        /// opened. Hence the `.pool` first, the legacy plain-string/array form
        /// second, then the pre-lane keys.
        /// </para>
        /// <para>
        /// Every candidate goes through <see cref="ProxyInputNormalizer"/> (the same
        /// authority the settings page uses), so `host:port` and
        /// `host:port:user:password` are accepted exactly as an operator may type
        /// them. A malformed entry degrades to "no proxy" with one warning: a bad
        /// config value must never be able to stop the desktop from starting.
        /// </para>
        /// </remarks>
        internal static string ResolveMailboxLaneProxy(ISettingsService settings, Serilog.ILogger logger)
        {
            foreach (string path in new[]
                     {
                         "proxy.lanes.mailbox.pool",
                         "proxy.lanes.mailbox",
                         "mailbox_proxy_pool",
                         "mailbox_proxy",
                     })
            {
                foreach (string? candidate in settings.GetStringList(path))
                {
                    string raw = (candidate ?? "").Trim();
                    if (raw.Length == 0)
                        continue;

                    try
                    {
                        string normalized = ProxyInputNormalizer.Normalize(raw);
                        if (normalized.Length > 0)
                            return normalized;
                    }
                    catch (FormatException ex)
                    {
                        // Host only: the failure reason carries no credentials, and
                        // the path says which key the operator has to look at.
                        logger.Warning(
                            "Mailbox lane proxy at {Path} is not a usable proxy ({Reason}); the request goes out directly",
                            path,
                            ex.Message);
                    }
                }
            }

            return "";
        }

        /// <summary>Refuses a non-allow-listed host and records one line per request.</summary>
        private sealed class EgressGuard : DelegatingHandler
        {
            private readonly Serilog.ILogger logger;
            private readonly bool viaProxy;

            internal EgressGuard(Serilog.ILogger logger, bool viaProxy)
            {
                this.logger = logger;
                this.viaProxy = viaProxy;
            }

            protected override async Task<HttpResponseMessage> SendAsync(
                HttpRequestMessage request,
                CancellationToken cancellationToken)
            {
                Uri? uri = request.RequestUri;
                if (!DesktopEgressAllowlist.IsAllowed(uri))
                {
                    // A refused host is a code defect, not a runtime condition: say so
                    // once, with the host only -- never the URL, which carries API keys.
                    logger.Error("Desktop egress refused a non-allow-listed host {Host}", uri?.Host ?? "");
                    throw new InvalidOperationException(
                        "host is not in the desktop egress allowlist: " + (uri?.Host ?? "(none)"));
                }

                long started = Environment.TickCount64;
                HttpResponseMessage response = await base.SendAsync(request, cancellationToken).ConfigureAwait(false);
                logger.Debug(
                    "Desktop egress {Host} {Status} {ElapsedMs}ms proxy={ViaProxy}",
                    uri!.Host,
                    (int)response.StatusCode,
                    Environment.TickCount64 - started,
                    viaProxy);
                return response;
            }
        }
    }
}
