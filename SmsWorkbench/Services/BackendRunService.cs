// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>What one finished backend run looks like on the task row.</summary>
    public sealed record BackendRunOutcome(string Status, string? Info, string CostSeconds, string DoneAt);

    /// <summary>
    /// The parts of a backend run that are not window decisions: the captured output
    /// buffer, the output-line folding (progress envelope vs. operator log), and the
    /// task-row outcome.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Split from <c>MainWindow.Tasks.cs</c> (2026-11-01). What stayed in the window is
    /// exactly what a service cannot own: the task row it started, the sidebar
    /// highlight, the progress dialog, the status bar, the pool refresh and the result
    /// panel. They arrive here as three delegates, so the folding rules can be tested
    /// without a window while the window keeps its own state.
    /// </para>
    /// <para>
    /// The capture buffer is the machine-readable stream (raw backend lines); the panel
    /// gets the folded operator story. Both are fed from the same line here so they
    /// cannot drift.
    /// </para>
    /// </remarks>
    public sealed class BackendRunService
    {
        private readonly StringBuilder captured = new();
        private readonly object gate = new();
        private readonly BackendLogFolder logFolder = new();

        /// <summary>Every raw line the backend emitted, in order.</summary>
        public string CapturedOutput
        {
            get
            {
                lock (gate)
                {
                    return captured.ToString();
                }
            }
        }

        /// <summary>Append one raw line to the machine-readable stream.</summary>
        public void Capture(string line)
        {
            lock (gate)
            {
                captured.AppendLine(line);
            }
        }

        /// <summary>
        /// Fold one backend output line. A progress envelope goes to
        /// <paramref name="onEvent"/> (metrics, throttled refresh, progress dialog) and
        /// its operator-facing stage line to <paramref name="onPanelLine"/>; an ordinary
        /// line is captured, folded by the log folder (its display lines go to
        /// <paramref name="onPanelLine"/>) and passed raw to <paramref name="onRawLine"/>
        /// for the hot-persistence detector.
        /// </summary>
        /// <remarks>
        /// The caller owns thread marshalling: the window wraps this in a
        /// <see cref="Progress{T}"/> so it runs on the UI thread, and a test calls it
        /// directly. An earlier version returned the <see cref="Progress{T}"/> itself,
        /// which made every assertion depend on a synchronisation context.
        /// </remarks>
        public void Feed(
            BackendOutputLine line,
            Action<BackendProgressEvent> onEvent,
            Action<string> onPanelLine,
            Action<string> onRawLine)
        {
            if (BackendProgressEventParser.TryParse(line.Text, out BackendProgressEvent? progressEvent))
            {
                onEvent(progressEvent);
                // The event itself never reaches the panel; emit its operator-facing
                // stage line here so the scan commands read as staged output instead of
                // a wall of raw backend lines.
                string? stageLine = BackendLogPresenter.ProgressEventLine(progressEvent);
                if (stageLine != null) onPanelLine(stageLine);
                return;
            }

            Capture(line.Text);
            foreach (string display in logFolder.Feed(line.Text))
            {
                onPanelLine(display);
            }
            onRawLine(line.Text);
        }

        /// <summary>
        /// Task-row outcome for a finished run. <paramref name="batchSummary"/> comes
        /// from <see cref="BackendResultInterpreter.BatchSummaryLabel"/> and wins over the
        /// interpreted display text when present.
        /// </summary>
        public static BackendRunOutcome Describe(
            BackendExecutionResult interpreted,
            string? batchSummary,
            DateTime started,
            DateTime finished)
        {
            string cost = ((int)(finished - started).TotalSeconds).ToString(CultureInfo.InvariantCulture);
            string doneAt = finished.ToString("yyyy-MM-dd HH:mm:ss");
            return new BackendRunOutcome(
                interpreted.IsSuccess ? "完成" : "失败",
                string.IsNullOrEmpty(batchSummary) ? null : batchSummary,
                cost,
                doneAt);
        }
    }
}
