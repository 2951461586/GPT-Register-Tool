// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench
{
    // ── Registration request models ────────────────────────────────────────
    //
    // Plain data with no window state. `RegisterOptions` / `ScanOptions` used to
    // live in `MainWindow.xaml.cs`; the two selection records were private
    // nested types of `MainWindow.Register.cs`. They sit next to the builder
    // that consumes them so the partial no longer carries them, and the field
    // names are unchanged because the dialogs, the planner calls and the tests
    // all read them by name.

    /// <summary>What the operator chose in the one-click registration dialog.</summary>
    public sealed class RegisterOptions
    {
        public string Source { get; set; } = "pool";
        public int Count { get; set; } = 1;
        public int Workers { get; set; } = 4;
        // Matches the dialog default (the "关闭 2FA" box starts unchecked). The
        // previous `= true` made every `new RegisterOptions()` silently disable
        // TOTP enrollment while the UI said otherwise.
        public bool Disable2fa { get; set; }
        public bool CheckPromotion { get; set; }
    }

    /// <summary>What the operator chose in the account-scan dialog.</summary>
    public sealed class ScanOptions
    {
        public int Workers { get; set; } = 4;
        public bool AutoRelogin { get; set; }
    }

    /// <summary>
    /// Result of building a temporary mailbox-file for a batch. Replaces the
    /// <c>out</c> parameters: an async method cannot declare them.
    /// </summary>
    /// <remarks>
    /// <see cref="File"/> is a batch file the builder created and it holds raw
    /// mailbox credential lines. Every caller must pass it to the plan as
    /// <c>temporaryFiles</c>; an undeclared batch file outlives the run in
    /// <c>%TEMP%</c>.
    /// </remarks>
    public sealed record MailboxFileSelection(string Arg, string File, int Count);

    /// <summary>
    /// Selection plus the row counts on both sides of the unregistered filter,
    /// which the caller needs to tell "nothing selected" apart from "selected
    /// rows have no usable mailbox line" -- and to show the operator how many
    /// checked rows the local filter dropped before the backend even ran.
    /// </summary>
    public sealed record PendingMailboxSelection(MailboxFileSelection? Selection, int PendingRowCount, int SelectedRowCount);
}
