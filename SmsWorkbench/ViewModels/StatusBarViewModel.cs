// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>The one-line status shown at the bottom of the main window.</summary>
    public sealed class StatusBarViewModel : ObservableObject
    {
        private string text = "就绪";

        public string Text
        {
            get => text;
            set => SetProperty(ref text, value ?? "");
        }
    }
}
