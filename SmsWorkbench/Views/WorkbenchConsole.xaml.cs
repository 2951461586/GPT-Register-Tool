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
        private readonly UIElement jumpToBottomButton;
        private readonly UIElement copyLogButton;
        private readonly UIElement clearLogButton;
        private readonly FrameworkElement consoleTabsTrack;
        private readonly Border activeTabIndicator;
        private readonly TranslateTransform activeTabSlide;
        private readonly RadioButton logTabButton;
        private readonly RadioButton tasksTabButton;
        private readonly StringBuilder pendingLogChunk = new();
        private readonly DispatcherTimer logFlushTimer;
        private ScrollViewer? logScrollViewer;
        private bool followLogTail = true;
        private bool isProgrammaticScroll;
        private bool updatingLogText;
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
            jumpToBottomButton = (UIElement)(
                FindName("JumpToBottomButton")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"JumpToBottomButton\""));
            copyLogButton = (UIElement)(
                FindName("CopyLogButton")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"CopyLogButton\""));
            clearLogButton = (UIElement)(
                FindName("ClearLogButton")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"ClearLogButton\""));
            consoleTabsTrack = (FrameworkElement)(
                FindName("ConsoleTabsTrack")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"ConsoleTabsTrack\""));
            activeTabIndicator = (Border)(
                FindName("ActiveTabIndicator")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"ActiveTabIndicator\""));
            activeTabSlide = (TranslateTransform)(
                FindName("ActiveTabSlide")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"ActiveTabSlide\""));
            logTabButton = (RadioButton)(
                FindName("LogTabButton")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"LogTabButton\""));
            tasksTabButton = (RadioButton)(
                FindName("TasksTabButton")
                ?? throw new InvalidOperationException("WorkbenchConsole.xaml is missing x:Name=\"TasksTabButton\""));
            logFlushTimer = new DispatcherTimer(
                TimeSpan.FromMilliseconds(50),
                DispatcherPriority.Background,
                (_, _) => FlushPendingLogs(),
                Dispatcher);
            logFlushTimer.Stop();
            Loaded += (_, _) => UpdateTabIndicator(animate: false);
            Unloaded += (_, _) =>
            {
                logFlushTimer.Stop();
                DetachLogScrollViewer();
            };
        }

        /// <summary>
        /// True while the live log automatically sticks to the newest (lowest) line.
        /// Dragging the scrollbar thumb upward pauses tail-following so the user can
        /// inspect earlier lines freely; scrolling back to the bottom or clicking
        /// <c>JumpToBottomButton</c> resumes it.
        /// </summary>
        public bool FollowLogTail => followLogTail;

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

            bool shouldFollow = followLogTail;
            ScrollViewer? sv = EnsureLogScrollViewer();
            double savedOffset = sv?.VerticalOffset ?? 0.0;

            if (pendingFullSync)
            {
                pendingFullSync = false;
                pendingLogChunk.Clear();
                ApplyLogTextUpdate(() => logTextBox.Text = LogPanel?.Text ?? "", shouldFollow, savedOffset);
                return;
            }

            if (pendingLogChunk.Length == 0)
            {
                return;
            }

            string chunk = pendingLogChunk.ToString();
            pendingLogChunk.Clear();
            ApplyLogTextUpdate(() => logTextBox.AppendText(chunk), shouldFollow, savedOffset);
        }

        /// <summary>Re-enables tail-following and scrolls the live log to the lowest line.</summary>
        public void JumpToLogBottom()
        {
            followLogTail = true;
            FlushPendingLogs();
            ScrollLogToBottom();
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

        private void LogTextBox_Loaded(object sender, RoutedEventArgs e)
        {
            EnsureLogScrollViewer();
            if (followLogTail)
            {
                ScrollLogToBottom();
            }
        }

        private void JumpToBottom_Click(object sender, RoutedEventArgs e) => JumpToLogBottom();

        private void LogTab_Click(object sender, RoutedEventArgs e)
        {
            SetActiveTab(showTasks: false);
            EnsureExpanded();
            FlushPendingLogs();
            if (followLogTail)
            {
                ScrollLogToBottom();
            }
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
            if (!IsCollapsed)
            {
                MotionBehaviors.PlaySoftEntrance(showingTasks ? tasksPane : logPane, offsetY: 8.0, durationMs: 150);
                if (!showingTasks)
                {
                    FlushPendingLogs();
                    if (followLogTail)
                    {
                        ScrollLogToBottom();
                    }
                }
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
            bool changed = showingTasks != showTasks;
            showingTasks = showTasks;
            logPane.Visibility = showTasks ? Visibility.Collapsed : Visibility.Visible;
            tasksPane.Visibility = showTasks ? Visibility.Visible : Visibility.Collapsed;
            UpdateLogActionButtonsVisibility();
            UpdateTabIndicator(animate: changed);
            if (changed)
            {
                MotionBehaviors.PlaySoftEntrance(showTasks ? tasksPane : logPane, offsetY: 3.0, durationMs: 140);
            }
        }

        private void ConsoleTabsTrack_SizeChanged(object sender, SizeChangedEventArgs e)
            => UpdateTabIndicator(animate: false);

        private void UpdateTabIndicator(bool animate)
        {
            RadioButton target = showingTasks ? tasksTabButton : logTabButton;
            if (target.ActualWidth <= 0 || consoleTabsTrack.ActualWidth <= 0)
            {
                return;
            }

            Point origin = target.TranslatePoint(new Point(0, 0), consoleTabsTrack);
            double targetX = Math.Max(0, origin.X);
            double targetWidth = target.ActualWidth;

            if (!animate || !SystemParameters.ClientAreaAnimation)
            {
                activeTabSlide.BeginAnimation(TranslateTransform.XProperty, null);
                activeTabIndicator.BeginAnimation(FrameworkElement.WidthProperty, null);
                activeTabSlide.X = targetX;
                activeTabIndicator.Width = targetWidth;
                return;
            }

            var ease = new System.Windows.Media.Animation.CubicEase
            {
                EasingMode = System.Windows.Media.Animation.EasingMode.EaseOut,
            };
            var duration = TimeSpan.FromMilliseconds(150);
            activeTabSlide.BeginAnimation(
                TranslateTransform.XProperty,
                new System.Windows.Media.Animation.DoubleAnimation(targetX, duration) { EasingFunction = ease });
            activeTabIndicator.BeginAnimation(
                FrameworkElement.WidthProperty,
                new System.Windows.Media.Animation.DoubleAnimation(targetWidth, duration) { EasingFunction = ease });
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
            jumpToBottomButton.Visibility = vis;
            copyLogButton.Visibility = vis;
            clearLogButton.Visibility = vis;
        }

        private void ApplyLogTextUpdate(Action mutate, bool shouldFollow, double savedOffset)
        {
            updatingLogText = true;
            try
            {
                mutate();
            }
            finally
            {
                updatingLogText = false;
            }

            if (shouldFollow)
            {
                ScrollLogToBottom();
            }
            else if (EnsureLogScrollViewer() is ScrollViewer sv && Math.Abs(sv.VerticalOffset - savedOffset) > 0.5)
            {
                isProgrammaticScroll = true;
                try
                {
                    sv.ScrollToVerticalOffset(savedOffset);
                }
                finally
                {
                    isProgrammaticScroll = false;
                }
            }
        }

        private void ScrollLogToBottom()
        {
            ScrollViewer? sv = EnsureLogScrollViewer();
            isProgrammaticScroll = true;
            try
            {
                if (sv is not null)
                {
                    sv.ScrollToBottom();
                }
                else
                {
                    logTextBox.ScrollToEnd();
                }
            }
            finally
            {
                isProgrammaticScroll = false;
            }
        }

        private ScrollViewer? EnsureLogScrollViewer()
        {
            if (logScrollViewer is not null)
            {
                return logScrollViewer;
            }

            logTextBox.ApplyTemplate();
            logScrollViewer = logTextBox.Template?.FindName("PART_ContentHost", logTextBox) as ScrollViewer
                ?? FindVisualDescendant<ScrollViewer>(logTextBox);
            if (logScrollViewer is not null)
            {
                logScrollViewer.ScrollChanged += LogScrollViewer_ScrollChanged;
            }

            return logScrollViewer;
        }

        private void DetachLogScrollViewer()
        {
            if (logScrollViewer is not null)
            {
                logScrollViewer.ScrollChanged -= LogScrollViewer_ScrollChanged;
                logScrollViewer = null;
            }
        }

        private void LogScrollViewer_ScrollChanged(object sender, ScrollChangedEventArgs e)
        {
            if (isProgrammaticScroll || sender is not ScrollViewer sv)
            {
                return;
            }

            if (!updatingLogText && e.ExtentHeightChange == 0 && e.ViewportHeightChange == 0)
            {
                // User scrolled manually (thumb drag, mouse wheel, keyboard): pause auto-follow
                // whenever moved away from the bottom, and seamlessly resume when back at the bottom.
                bool atBottom = sv.ScrollableHeight <= 1.0
                    || sv.VerticalOffset >= sv.ScrollableHeight - 4.0;
                followLogTail = atBottom;
                return;
            }

            if ((e.ExtentHeightChange != 0 || e.ViewportHeightChange != 0) && followLogTail)
            {
                ScrollLogToBottom();
            }
        }

        private static T? FindVisualDescendant<T>(DependencyObject root) where T : DependencyObject
        {
            int count = VisualTreeHelper.GetChildrenCount(root);
            for (int i = 0; i < count; i++)
            {
                DependencyObject child = VisualTreeHelper.GetChild(root, i);
                if (child is T match)
                {
                    return match;
                }

                T? nested = FindVisualDescendant<T>(child);
                if (nested is not null)
                {
                    return nested;
                }
            }

            return null;
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
            followLogTail = true;
            logTextBox?.Clear();
        }
    }
}
