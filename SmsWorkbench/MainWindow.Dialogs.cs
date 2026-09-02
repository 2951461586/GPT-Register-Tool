// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        private void ShowThemedInfoDialog(string title, string message)
            => RunUiTask(() => DialogFactory.ShowInfoAsync(this, title, message));
    }
}
