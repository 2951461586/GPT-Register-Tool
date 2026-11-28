using System.Windows.Input;

namespace SmsWorkbench.Views
{
    public partial class SecondaryTitleBar : UserControl
    {
        public static readonly DependencyProperty TitleTextProperty = DependencyProperty.Register(
            nameof(TitleText), typeof(string), typeof(SecondaryTitleBar), new PropertyMetadata(string.Empty));

        public static readonly DependencyProperty SubtitleTextProperty = DependencyProperty.Register(
            nameof(SubtitleText), typeof(string), typeof(SecondaryTitleBar), new PropertyMetadata(string.Empty));

        public static readonly DependencyProperty BadgeTextProperty = DependencyProperty.Register(
            nameof(BadgeText), typeof(string), typeof(SecondaryTitleBar), new PropertyMetadata(string.Empty));

        public static readonly DependencyProperty IsWindowMaximizedProperty = DependencyProperty.Register(
            nameof(IsWindowMaximized), typeof(bool), typeof(SecondaryTitleBar), new PropertyMetadata(false));

        private Window? hostWindow;

        public SecondaryTitleBar()
        {
            InitializeComponent();
            Loaded += OnLoaded;
            Unloaded += OnUnloaded;
        }

        public string TitleText
        {
            get => (string)GetValue(TitleTextProperty);
            set => SetValue(TitleTextProperty, value);
        }

        public string SubtitleText
        {
            get => (string)GetValue(SubtitleTextProperty);
            set => SetValue(SubtitleTextProperty, value);
        }

        public string BadgeText
        {
            get => (string)GetValue(BadgeTextProperty);
            set => SetValue(BadgeTextProperty, value);
        }

        public bool IsWindowMaximized
        {
            get => (bool)GetValue(IsWindowMaximizedProperty);
            set => SetValue(IsWindowMaximizedProperty, value);
        }

        private void OnLoaded(object sender, RoutedEventArgs e)
        {
            hostWindow = Window.GetWindow(this);
            if (hostWindow is null) return;
            hostWindow.StateChanged -= OnHostStateChanged;
            hostWindow.StateChanged += OnHostStateChanged;
            IsWindowMaximized = hostWindow.WindowState == WindowState.Maximized;
        }

        private void OnUnloaded(object sender, RoutedEventArgs e)
        {
            if (hostWindow is not null)
            {
                hostWindow.StateChanged -= OnHostStateChanged;
                hostWindow = null;
            }
        }

        private void OnHostStateChanged(object? sender, EventArgs e)
        {
            if (hostWindow is not null)
            {
                IsWindowMaximized = hostWindow.WindowState == WindowState.Maximized;
            }
        }

        private void Header_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            Window? win = hostWindow ?? Window.GetWindow(this);
            if (win is null) return;
            if (e.ClickCount == 2 && win.ResizeMode != ResizeMode.NoResize)
            {
                ToggleMaximize(win);
                return;
            }
            if (e.LeftButton == MouseButtonState.Pressed)
            {
                try { win.DragMove(); } catch { }
            }
        }

        private void Minimize_Click(object sender, RoutedEventArgs e)
        {
            Window? win = hostWindow ?? Window.GetWindow(this);
            if (win is not null) win.WindowState = WindowState.Minimized;
        }

        private void Maximize_Click(object sender, RoutedEventArgs e)
        {
            Window? win = hostWindow ?? Window.GetWindow(this);
            if (win is not null && win.ResizeMode != ResizeMode.NoResize) ToggleMaximize(win);
        }

        private void Close_Click(object sender, RoutedEventArgs e)
        {
            (hostWindow ?? Window.GetWindow(this))?.Close();
        }

        private static void ToggleMaximize(Window win)
        {
            win.WindowState = win.WindowState == WindowState.Maximized
                ? WindowState.Normal
                : WindowState.Maximized;
        }
    }
}
