namespace SmsWorkbench.ViewModels
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
        // Trim down to 80% capacity when the ceiling is crossed so subsequent
        // lines remain O(1) appends instead of re-trimming and re-rendering the
        // entire TextBox on every single line after 1 MB.
        internal const int TrimTargetChars = 800_000;

        private readonly StringBuilder buffer = new();

        public LogViewModel()
        {
            ClearCommand = new RelayCommand(Clear);
        }

        public RelayCommand ClearCommand { get; }

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
        /// bounded with hysteresis headroom. Callers still push into the TextBox
        /// directly, so appending here raises no change notification.
        /// Returns true when the oldest text was dropped, so the view can resync.
        /// </summary>
        public bool Append(string line)
        {
            buffer.Append(line);
            if (buffer.Length <= MaxBufferChars) return false;
            int removeCount = Math.Max(buffer.Length - TrimTargetChars, buffer.Length - MaxBufferChars);
            int maxScan = Math.Min(buffer.Length, removeCount + 2048);
            for (int i = removeCount; i < maxScan; i++)
            {
                if (buffer[i] == '\n')
                {
                    removeCount = i + 1;
                    break;
                }
            }
            if (removeCount >= buffer.Length)
            {
                removeCount = buffer.Length - TrimTargetChars;
            }
            buffer.Remove(0, Math.Max(0, removeCount));
            return true;
        }

        /// <summary>Raised after <see cref="Clear"/> so the view can empty the TextBox it also appends to directly.</summary>
        public event EventHandler? Cleared;

        public void Clear()
        {
            Text = "";
            Cleared?.Invoke(this, EventArgs.Empty);
        }

        public static string FormatLine(DateTime now, string safeText)
            => "[" + now.ToString("HH:mm:ss", CultureInfo.InvariantCulture) + "] " + safeText + Environment.NewLine;
    }
}
