namespace SmsWorkbench.Views
{
    /// <summary>
    /// Task list + log console.
    ///
    /// The panel used to be addressed by name from three MainWindow partials
    /// (`TaskGrid`, `LogTextBox`). It now exposes the three operations those
    /// partials actually needed — scroll to the last task, append a rendered
    /// line, and clear on the view model's event — and keeps the widgets private
    /// to its own namescope.
    /// </summary>
    public partial class WorkbenchConsole : UserControl
    {
        public static readonly DependencyProperty TaskListProperty = DependencyProperty.Register(
            nameof(TaskList),
            typeof(TaskListViewModel),
            typeof(WorkbenchConsole),
            new PropertyMetadata(null));

        public static readonly DependencyProperty LogPanelProperty = DependencyProperty.Register(
            nameof(LogPanel),
            typeof(LogViewModel),
            typeof(WorkbenchConsole),
            new PropertyMetadata(null, OnLogPanelChanged));

        // Resolved by name instead of using the generated fields: per-file
        // analyzers (and a fresh LSP) do not see WorkbenchConsole.g.cs.
        private readonly System.Windows.Controls.DataGrid taskGrid;
        private readonly System.Windows.Controls.TextBox logTextBox;
        private readonly StringBuilder pendingLogChunk = new();
        private readonly DispatcherTimer logFlushTimer;
        private bool pendingFullSync;
        private LogViewModel? _attachedLogPanel;

        public WorkbenchConsole()
        {
            InitializeComponent();
            taskGrid = (System.Windows.Controls.DataGrid)(
                FindName("TaskGrid")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"TaskGrid\""));
            logTextBox = (System.Windows.Controls.TextBox)(
                FindName("LogTextBox")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"LogTextBox\""));
            logFlushTimer = new DispatcherTimer(
                TimeSpan.FromMilliseconds(50),
                DispatcherPriority.Background,
                (_, _) => FlushPendingLogs(),
                Dispatcher);
            logFlushTimer.Stop();
            Unloaded += (_, _) => logFlushTimer.Stop();
        }

        public TaskListViewModel? TaskList
        {
            get => (TaskListViewModel?)GetValue(TaskListProperty);
            set => SetValue(TaskListProperty, value);
        }

        public LogViewModel? LogPanel
        {
            get => (LogViewModel?)GetValue(LogPanelProperty);
            set => SetValue(LogPanelProperty, value);
        }

        /// <summary>
        /// Appends an already-redacted line into the view model immediately and
        /// batches the TextBox visual update on a short UI-frame timer so rapid
        /// backend bursts do not trigger per-line layout passes.
        /// Returns false while the control is not loaded/realised, so the caller
        /// keeps the old "append to the view model only" fallback.
        /// </summary>
        public bool TryAppendLogLine(string line)
        {
            if (logTextBox is not { IsLoaded: true })
            {
                return false;
            }

            // Update the view-model buffer immediately (O(1) amortized) so
            // synchronous readers see every line right away, while coalescing
            // TextBox writes into a 50ms UI batch.
            if (LogPanel?.Append(line) == true)
            {
                pendingFullSync = true;
                pendingLogChunk.Clear();
            }
            else if (!pendingFullSync)
            {
                pendingLogChunk.Append(line);
            }

            if (!logFlushTimer.IsEnabled)
            {
                logFlushTimer.Start();
            }

            return true;
        }

        internal void FlushPendingLogs()
        {
            logFlushTimer.Stop();
            if (logTextBox is not { IsLoaded: true })
            {
                pendingLogChunk.Clear();
                pendingFullSync = false;
                return;
            }

            if (pendingFullSync)
            {
                pendingFullSync = false;
                pendingLogChunk.Clear();
                logTextBox.Text = LogPanel?.Text ?? "";
                return;
            }

            if (pendingLogChunk.Length == 0)
            {
                return;
            }

            string chunk = pendingLogChunk.ToString();
            pendingLogChunk.Clear();
            logTextBox.AppendText(chunk);
        }

        /// <summary>Selects and reveals the newest task row (posted to the UI queue).</summary>
        public void ScrollToLast()
        {
            if (TaskList?.Last is null)
            {
                return;
            }

            Dispatcher.BeginInvoke(
                new Action(() =>
                {
                    object? last = TaskList.Last;
                    if (last is null)
                    {
                        return;
                    }

                    taskGrid.SelectedItem = last;
                    taskGrid.ScrollIntoView(last);
                }),
                DispatcherPriority.Background);
        }

        private void TaskGrid_Loaded(object sender, RoutedEventArgs e) => ScrollToLast();

        private static void OnLogPanelChanged(DependencyObject dependencyObject, DependencyPropertyChangedEventArgs e)
        {
            var console = (WorkbenchConsole)dependencyObject;
            if (console._attachedLogPanel is not null)
            {
                console._attachedLogPanel.Cleared -= console.OnLogCleared;
            }

            console._attachedLogPanel = e.NewValue as LogViewModel;
            if (console._attachedLogPanel is not null)
            {
                console._attachedLogPanel.Cleared += console.OnLogCleared;
            }
        }

        private void OnLogCleared(object? sender, EventArgs e)
        {
            logFlushTimer.Stop();
            pendingLogChunk.Clear();
            pendingFullSync = false;
            logTextBox?.Clear();
        }
    }
}
