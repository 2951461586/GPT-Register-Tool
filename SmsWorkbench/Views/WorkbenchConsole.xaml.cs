using System.Runtime.InteropServices;

namespace SmsWorkbench.Views
{
    /// <summary>
    /// Collapsible segmented task list + live log console drawer.
    ///
    /// Keeps <c>TaskGrid</c> and <c>LogTextBox</c> inside its own namescope,
    /// switches between the live terminal log and batch task queue via segmented
    /// tabs, and notifies <see cref="MainWindow"/> through
    /// <see cref="CollapseToggled"/> when collapsed or expanded.
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

        public static readonly DependencyProperty IsCollapsedProperty = DependencyProperty.Register(
            nameof(IsCollapsed),
            typeof(bool),
            typeof(WorkbenchConsole),
            new PropertyMetadata(false));

        public static readonly RoutedEvent CollapseToggledEvent = EventManager.RegisterRoutedEvent(
            nameof(CollapseToggled),
            RoutingStrategy.Bubble,
            typeof(RoutedEventHandler),
            typeof(WorkbenchConsole));

        // Resolved by name instead of using generated fields: per-file
        // analyzers (and a fresh LSP) do not see WorkbenchConsole.g.cs.
        private readonly System.Windows.Controls.DataGrid taskGrid;
        private readonly System.Windows.Controls.TextBox logTextBox;
        private readonly UIElement logPane;
        private readonly UIElement tasksPane;
        private readonly UIElement copyLogButton;
        private readonly UIElement clearLogButton;
        private readonly StringBuilder pendingLogChunk = new();
        private readonly DispatcherTimer logFlushTimer;
        private bool pendingFullSync;
        private bool showingTasks;
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
            logPane = (UIElement)(
                FindName("LogPane")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"LogPane\""));
            tasksPane = (UIElement)(
                FindName("TasksPane")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"TasksPane\""));
            copyLogButton = (UIElement)(
                FindName("CopyLogButton")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"CopyLogButton\""));
            clearLogButton = (UIElement)(
                FindName("ClearLogButton")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"ClearLogButton\""));
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

        public bool IsCollapsed
        {
            get => (bool)GetValue(IsCollapsedProperty);
            set => SetValue(IsCollapsedProperty, value);
        }

        public event RoutedEventHandler CollapseToggled
        {
            add => AddHandler(CollapseToggledEvent, value);
            remove => RemoveHandler(CollapseToggledEvent, value);
        }

        /// <summary>
        /// Appends an already-redacted line into the view model immediately and
        /// batches the TextBox visual update on a short UI-frame timer so rapid
        /// backend bursts do not trigger per-line layout passes.
        /// </summary>
        public bool TryAppendLogLine(string line)
        {
            if (logTextBox is not { IsLoaded: true })
            {
                return false;
            }

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

        private void LogTab_Click(object sender, RoutedEventArgs e)
        {
            SetActiveTab(showTasks: false);
            EnsureExpanded();
            FlushPendingLogs();
        }

        private void TasksTab_Click(object sender, RoutedEventArgs e)
        {
            SetActiveTab(showTasks: true);
            EnsureExpanded();
            ScrollToLast();
        }

        private void CollapseToggle_Click(object sender, RoutedEventArgs e)
        {
            IsCollapsed = !IsCollapsed;
            UpdateLogActionButtonsVisibility();
            RaiseEvent(new RoutedEventArgs(CollapseToggledEvent, this));
            if (!IsCollapsed && !showingTasks)
            {
                FlushPendingLogs();
            }
        }

        private void CopyLog_Click(object sender, RoutedEventArgs e)
        {
            string text = LogPanel?.Text ?? logTextBox.Text ?? "";
            if (text.Length == 0)
            {
                return;
            }

            try
            {
                Clipboard.SetText(text);
            }
            catch (ExternalException)
            {
                // Clipboard may be locked transiently by another Windows process.
            }
        }

        private void SetActiveTab(bool showTasks)
        {
            showingTasks = showTasks;
            logPane.Visibility = showTasks ? Visibility.Collapsed : Visibility.Visible;
            tasksPane.Visibility = showTasks ? Visibility.Visible : Visibility.Collapsed;
            UpdateLogActionButtonsVisibility();
        }

        private void EnsureExpanded()
        {
            if (!IsCollapsed)
            {
                return;
            }

            IsCollapsed = false;
            UpdateLogActionButtonsVisibility();
            RaiseEvent(new RoutedEventArgs(CollapseToggledEvent, this));
        }

        private void UpdateLogActionButtonsVisibility()
        {
            Visibility vis = (!IsCollapsed && !showingTasks) ? Visibility.Visible : Visibility.Collapsed;
            copyLogButton.Visibility = vis;
            clearLogButton.Visibility = vis;
        }

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
