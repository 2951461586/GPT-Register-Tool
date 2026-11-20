// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>One "label over value" card in the account-detail summary grid.</summary>
    public sealed record AccountDetailSummaryItem(string Label, string Value);

    /// <summary>
    /// Everything the account-detail dialog renders, assembled outside WPF.
    /// </summary>
    /// <remarks>
    /// Split out of <c>MainWindow.Detail.cs</c> on 2026-11-20 together with the
    /// dialog itself. The partial used to build ~250 lines of Grid/StackPanel/
    /// Border in code; the shape of the data is now a plain object, so the dialog
    /// can be instantiated and asserted in <c>dotnet test</c> without a live
    /// account, and the layout is editable in XAML.
    /// </remarks>
    public sealed class AccountDetailView
    {
        public string Identifier { get; init; } = "";
        public string Status { get; init; } = "";
        public bool HasStatus => !string.IsNullOrWhiteSpace(Status);
        public IReadOnlyList<AccountDetailSummaryItem> SummaryItems { get; init; } = Array.Empty<AccountDetailSummaryItem>();
        public string PayPalUrl { get; init; } = "";
        public bool HasPayPalUrl => !string.IsNullOrWhiteSpace(PayPalUrl);
        public string DetailText { get; init; } = "";
        public string AccessToken { get; init; } = "";
        public bool HasAccessToken => !string.IsNullOrWhiteSpace(AccessToken);
    }

    /// <summary>
    /// The window actions the detail dialog can trigger.
    /// </summary>
    /// <remarks>
    /// The dialog does not know about <see cref="MainWindow"/>: the buttons used to
    /// capture the window through lambdas inside the partial, which is why the
    /// panel could never be tested. Each member is a command the window already
    /// owns (<c>OpenAccountJson</c>, <c>OpenPayPalUrl</c>, <c>CopyPayPalUrl</c>,
    /// <c>CheckAccountAliveAsync</c>).
    /// </remarks>
    public interface IAccountDetailActions
    {
        /// <summary>Open the account's session JSON in the OS default handler.</summary>
        void OpenSourceFile(PoolRow row);

        /// <summary>Open the PayPal payment URL in the browser.</summary>
        void OpenPayPal(PoolRow row);

        /// <summary>Copy the PayPal payment URL to the clipboard, with feedback.</summary>
        void CopyPayPal(PoolRow row);

        /// <summary>Close the dialog and run the account-liveness probe.</summary>
        void CheckAlive(PoolRow row);
    }
}
