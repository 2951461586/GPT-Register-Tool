// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// One bucket of the dashboard's failure split.
    /// </summary>
    /// <param name="FailureClass">
    /// The backend's own failure class (<c>otp_timeout</c>, <c>auth_state</c>, ...), or
    /// the localized "unknown" bucket when the event carried none. Machine names are
    /// kept verbatim on purpose: they match the log lines an operator greps.
    /// </param>
    /// <param name="Count">Finished attempts in that class, inside the window.</param>
    public sealed record FailureSlice(string FailureClass, int Count);
}
