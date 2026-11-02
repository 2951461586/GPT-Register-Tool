// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using System.Text.Json.Nodes;

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // One-click SMS rental dialog. The dialog itself (catalog read, country/tier
        // list, config write-back) lives in SmsProviderDialogService; this partial only
        // keeps the entry point so callers did not change.
        private readonly ISmsProviderDialogService smsDialogs;

        private Task<bool> ShowSmsProviderOneClickDialogAsync(CancellationToken ct = default)
            => smsDialogs.ShowOneClickDialogAsync(this, ct);

    }
}
