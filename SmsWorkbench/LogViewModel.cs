// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Retained text of the main window's log panel.
    /// </summary>
    /// <remarks>
    /// Log accumulation used to be <c>logText += line</c> on every backend line,
    /// a full-string copy per line (O(n^2) over a run) that was never truncated.
    /// A <see cref="StringBuilder"/> gives amortised O(1) appends and
    /// <see cref="MaxBufferChars"/> caps the retained history.
    /// Not thread-safe by design: every caller reaches this from the UI thread
    /// (Progress&lt;T&gt; callbacks marshal back to the Dispatcher context); the old
    /// string concatenation had exactly the same contract.
    /// </remarks>
    public sealed class LogViewModel : ObservableObject
    {
        internal const int MaxBufferChars = 1_000_000;

        private readonly StringBuilder buffer = new();

        public string Text
        {
            get => buffer.ToString();
            set
            {
                buffer.Clear();
                if (!string.IsNullOrEmpty(value)) buffer.Append(value);
                OnPropertyChanged(nameof(Text));
            }
        }

        /// <summary>
        /// Appends one already-formatted log line and keeps the retained history
        /// bounded. Callers still push into the TextBox directly, so appending
        /// here raises no change notification (and triggers no re-render).
        /// Returns true when the oldest text was dropped, so the view can resync.
        /// </summary>
        public bool Append(string line)
        {
            buffer.Append(line);
            if (buffer.Length <= MaxBufferChars) return false;
            buffer.Remove(0, buffer.Length - MaxBufferChars);
            return true;
        }

        public void Clear() => Text = "";

        public static string FormatLine(DateTime now, string safeText)
            => "[" + now.ToString("HH:mm:ss") + "] " + safeText + Environment.NewLine;
    }
}
