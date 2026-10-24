// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
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
        /// Appends an already-redacted line straight into the TextBox.
        /// Returns false while the control is not loaded/realised, so the caller
        /// keeps the old "append to the view model only" fallback.
        /// </summary>
        public bool TryAppendLogLine(string line)
        {
            if (logTextBox is not { IsLoaded: true })
            {
                return false;
            }

            // Append into the control directly: whole-string reassignment of
            // LogText re-rendered the entire buffer on every line (O(n²)).
            logTextBox.AppendText(line);
            // Keep the bound property consistent without re-rendering;
            // Append caps retained history (see LogViewModel).
            if (LogPanel?.Append(line) == true)
            {
                logTextBox.Text = LogPanel.Text;
            }

            return true;
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

        private void OnLogCleared(object? sender, EventArgs e) => logTextBox?.Clear();
    }
}
