// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// One message from the backend inbox, as the mail-list and mail-detail dialogs
    /// render it.
    /// </summary>
    /// <remarks>
    /// Was a private nested class of <c>MainWindow.Inbox.cs</c>; moved out with the
    /// inbox split (2026-11-01) so the model is not owned by a window partial. The
    /// field names match the backend JSON keys the reader fills them from.
    /// </remarks>
    public sealed class MailItem
    {
        public string ReceivedAt { get; set; } = "";
        public string From { get; set; } = "";
        public string Subject { get; set; } = "";
        public string BodyPreview { get; set; } = "";
        public string Body { get; set; } = "";
        public string VerificationCode { get; set; } = "";
    }
}
