// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Sidebar visuals (brand, navigation, collapse button).
    ///
    /// The column width and its animation stay on <see cref="MainWindow"/>:
    /// <c>SidebarColumn</c> is a column definition of the window grid, so the
    /// control cannot own it. <see cref="IsCollapsed"/> drives every visual
    /// trigger inside the control; the button only raises
    /// <see cref="ToggleRequested"/> and lets the window flip
    /// <c>SidebarCollapsed</c>, which keeps one owner for the state.
    /// </summary>
    public partial class Sidebar : UserControl
    {
        public static readonly DependencyProperty IsCollapsedProperty = DependencyProperty.Register(
            nameof(IsCollapsed),
            typeof(bool),
            typeof(Sidebar),
            new PropertyMetadata(false));

        /// <summary>
        /// <c>CommandParameter</c> of the row whose action is running, or empty.
        /// Each row compares it against its own parameter (see
        /// <c>IconNavButtonStyle</c>); the sidebar owns no state of its own.
        /// </summary>
        public static readonly DependencyProperty ActiveKeyProperty = DependencyProperty.Register(
            nameof(ActiveKey),
            typeof(string),
            typeof(Sidebar),
            new PropertyMetadata(string.Empty));

        public static readonly RoutedEvent ToggleRequestedEvent = EventManager.RegisterRoutedEvent(
            nameof(ToggleRequested),
            RoutingStrategy.Bubble,
            typeof(RoutedEventHandler),
            typeof(Sidebar));

        public Sidebar()
        {
            InitializeComponent();
        }

        public bool IsCollapsed
        {
            get => (bool)GetValue(IsCollapsedProperty);
            set => SetValue(IsCollapsedProperty, value);
        }

        public string ActiveKey
        {
            get => (string)GetValue(ActiveKeyProperty);
            set => SetValue(ActiveKeyProperty, value);
        }

        public event RoutedEventHandler ToggleRequested
        {
            add => AddHandler(ToggleRequestedEvent, value);
            remove => RemoveHandler(ToggleRequestedEvent, value);
        }

        private void Toggle_Click(object sender, RoutedEventArgs e)
            => RaiseEvent(new RoutedEventArgs(ToggleRequestedEvent, this));
    }
}
