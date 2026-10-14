// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Row cache, filter, paging, sorting and selection live in PoolViewModel
        // (MainWindow.Pool); only the refresh-in-flight flag is view-level.
        private bool poolsRefreshRunning;
    }
}
