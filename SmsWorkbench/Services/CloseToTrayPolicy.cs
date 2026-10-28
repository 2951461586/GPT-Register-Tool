// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Whether a window close request should hide the shell instead of shutting it down.
    /// </summary>
    /// <remarks>
    /// The decision is one line, and it is separated from the window for exactly that
    /// reason: <c>MainWindow.OnWindowClosing</c> cannot be exercised for the "hide"
    /// branch in a smoke test without also exercising <c>Hide</c>, whereas the truth
    /// table here is testable on its own and states the contract:
    ///
    /// * no tray icon (a headless session, a failed registration) -> close, because
    ///   hiding would leave a running process with no way back;
    /// * quit requested from the tray menu -> close, that is what "quit" means;
    /// * otherwise -> hide, so a stray click on the X cannot abort a running batch.
    /// </remarks>
    internal static class CloseToTrayPolicy
    {
        public static bool ShouldHideToTray(bool trayAvailable, bool exitRequested)
            => trayAvailable && !exitRequested;
    }
}
