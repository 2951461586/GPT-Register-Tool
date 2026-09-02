namespace SmsWorkbench
{
    /// <summary>
    /// Wire contract for the resident desktop-read channel
    /// (<c>python chatgpt_phone_reg.py --desktop-serve</c>), shared by the WPF
    /// client and the Python server.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The channel is one JSON object per line on stdin/stdout. Before this
    /// contract existed the client had no way to tell "backend script is from a
    /// different release" apart from "backend is wedged": both surfaced as an
    /// opaque <c>error</c> string, and a partially-written request left the
    /// pending-response dictionary entry in place forever.
    /// </para>
    /// <para>
    /// Rules: <b>additive only</b>. A new field is always optional for the
    /// reader, so an old client talking to a new backend (or vice versa) degrades
    /// instead of throwing. Bumping <see cref="Version"/> is what forces both
    /// sides back to the slow one-shot path.
    /// </para>
    /// </remarks>
    public static class DesktopReadProtocol
    {
        /// <summary>
        /// Bump when a change is not backward compatible. The client refuses a
        /// backend that reports a different value and falls back to one-shot
        /// reads rather than misreading the payloads.
        /// </summary>
        public const int Version = 1;

        /// <summary>Handshake op: returns the backend's protocol version and op set.</summary>
        public const string OpHello = "hello";

        /// <summary>Liveness probe. Must stay side-effect free and cheap.</summary>
        public const string OpPing = "ping";

        /// <summary>Per-request ceiling for normal ops.</summary>
        public static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(120);

        /// <summary>
        /// Handshake ceiling. Deliberately much shorter than
        /// <see cref="RequestTimeout"/>: a backend that cannot answer
        /// <c>hello</c> in 15s will not answer a real read either, and failing
        /// fast here costs one cold start instead of 120s of a wedged UI.
        /// </summary>
        public static readonly TimeSpan HandshakeTimeout = TimeSpan.FromSeconds(15);

        /// <summary>
        /// A channel idle longer than this is probed before reuse. A resident
        /// process can be alive (<see cref="System.Diagnostics.Process.HasExited"/>
        /// false) yet wedged on a blocked read; without the probe the wedged
        /// process is handed the next request and only the 120s timeout reveals
        /// it.
        /// </summary>
        public static readonly TimeSpan HeartbeatIdleThreshold = TimeSpan.FromSeconds(45);

        /// <summary>
        /// Ceiling for the idle probe. Shorter than
        /// <see cref="RequestTimeout"/>: the probe exists to fail fast, and a
        /// healthy backend answers <c>ping</c> in milliseconds.
        /// </summary>
        public static readonly TimeSpan HeartbeatTimeout = TimeSpan.FromSeconds(10);

        /// <summary>Response field carrying the machine-readable error code.</summary>
        public const string CodeField = "code";
    }

    /// <summary>
    /// Stable, machine-readable failure classes for the desktop-read channel.
    /// The wire form is the snake_case name of the enum member so the Python
    /// side can emit them without a generated table.
    /// </summary>
    public enum DesktopReadErrorCode
    {
        /// <summary>No error, or a response that predates error codes.</summary>
        None = 0,

        /// <summary>The request line was not a JSON object.</summary>
        BadRequest,

        /// <summary>The op is not one the backend implements (usually a version skew).</summary>
        UnknownOperation,

        /// <summary>The op was understood but the underlying read failed.</summary>
        BackendError,

        /// <summary>
        /// The Python watchdog killed the request. Means the handler blocked
        /// rather than returned; retrying the same op is unlikely to help.
        /// </summary>
        WatchdogTimeout,

        /// <summary>Unclassified server-side failure.</summary>
        Internal,

        // ── Client-side codes (never sent by the backend) ──────────────

        /// <summary>No response within <see cref="DesktopReadProtocol.RequestTimeout"/>.</summary>
        Timeout,

        /// <summary>The caller's cancellation token fired.</summary>
        Cancelled,

        /// <summary>Handshake reported a protocol version this client cannot speak.</summary>
        ProtocolMismatch,

        /// <summary>The channel died or could not be started; caller should fall back.</summary>
        ChannelUnavailable,
    }

    /// <summary>Mapping between <see cref="DesktopReadErrorCode"/> and its wire string.</summary>
    public static class DesktopReadErrorCodes
    {
        public static string ToWire(DesktopReadErrorCode code) => code switch
        {
            DesktopReadErrorCode.BadRequest => "bad_request",
            DesktopReadErrorCode.UnknownOperation => "unknown_operation",
            DesktopReadErrorCode.BackendError => "backend_error",
            DesktopReadErrorCode.WatchdogTimeout => "watchdog_timeout",
            DesktopReadErrorCode.Internal => "internal",
            DesktopReadErrorCode.Timeout => "timeout",
            DesktopReadErrorCode.Cancelled => "cancelled",
            DesktopReadErrorCode.ProtocolMismatch => "protocol_mismatch",
            DesktopReadErrorCode.ChannelUnavailable => "channel_unavailable",
            _ => "none",
        };

        public static DesktopReadErrorCode Parse(string? wire)
        {
            if (string.IsNullOrWhiteSpace(wire)) return DesktopReadErrorCode.None;
            return wire!.Trim().ToLowerInvariant() switch
            {
                "bad_request" => DesktopReadErrorCode.BadRequest,
                "unknown_operation" => DesktopReadErrorCode.UnknownOperation,
                "unknown_op" => DesktopReadErrorCode.UnknownOperation,
                "backend_error" => DesktopReadErrorCode.BackendError,
                "watchdog_timeout" => DesktopReadErrorCode.WatchdogTimeout,
                "internal" => DesktopReadErrorCode.Internal,
                "timeout" => DesktopReadErrorCode.Timeout,
                "cancelled" => DesktopReadErrorCode.Cancelled,
                "protocol_mismatch" => DesktopReadErrorCode.ProtocolMismatch,
                "channel_unavailable" => DesktopReadErrorCode.ChannelUnavailable,
                _ => DesktopReadErrorCode.None,
            };
        }
    }
}
