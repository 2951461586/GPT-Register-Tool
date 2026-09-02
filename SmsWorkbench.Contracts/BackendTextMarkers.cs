namespace SmsWorkbench
{
    /// <summary>
    /// Free-text markers the C# host matches against the Python backend's stdout.
    ///
    /// This is the weakest possible contract: a bare substring with no version,
    /// no schema and no failure signal. If the Python <c>print()</c> that emits
    /// one of these changes, the corresponding C# behaviour silently stops
    /// happening and nothing reports an error.
    ///
    /// Prefer a real <see cref="BackendProgressEvent"/> envelope (which carries a
    /// version and a schema and is rejected on mismatch) whenever a new signal is
    /// added. The entries below exist only for markers that predate the IPC
    /// protocol and have not been migrated yet.
    ///
    /// Every constant here MUST stay byte-identical to its Python counterpart.
    /// <c>tests/test_backend_text_markers.py</c> parses both sides and asserts
    /// equality, so a one-sided edit fails the suite instead of failing silently.
    /// </summary>
    public static class BackendTextMarkers
    {
        /// <summary>
        /// Emitted by <c>sms_tool.commands.registration</c> right after a session
        /// file is written. Drives the hot-persistence pool refresh on the host.
        /// </summary>
        public const string SavedSession = "Saved session:";

        /// <summary>
        /// Substrings meaning "this account is gone". Mirrors
        /// <c>ACCOUNT_DEACTIVATED_MARKERS</c> in <c>sms_tool.store.normalize</c>.
        /// </summary>
        public static readonly string[] AccountDeactivated =
        {
            "account_deactivated",
            // A misspelling emitted by an older release; session files already on
            // disk still carry it, so this is live data rather than a typo.
            "account_deatived",
            "deleted or deactivated",
            "account has been deleted",
            "account has been deactivated",
        };

        /// <summary>
        /// Substrings meaning "the stored access token can no longer be used".
        /// Mirrors <c>AT_INVALID_MARKERS</c> in <c>sms_tool.store.normalize</c>,
        /// which is this list plus <see cref="AccountDeactivated"/>.
        /// </summary>
        public static readonly string[] AtInvalid =
        {
            "token_invalidated",
            "token_expired",
            "authentication token has been invalidated",
            "could not validate your token",
            "add_phone_required",
            "secondary_phone_verification_required",
            "oauth_refresh_http_401",
        };
    }
}
