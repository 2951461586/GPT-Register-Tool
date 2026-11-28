using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;

namespace SmsWorkbench.Views
{
    /// <summary>
    /// Lightweight, native-WPF micro-interaction attached behaviors inspired by
    /// restrained productivity patterns (CountUp numeric transitions, subtle
    /// pointer-tracking radial spotlight glow, and soft panel entrance).
    /// Every animation honours <see cref="SystemParameters.ClientAreaAnimation"/>.
    /// </summary>
    public static class MotionBehaviors
    {
        private const int CountUpDurationMs = 220;

        // ── Placeholder (subtle muted hint for empty TextBoxes) ──────────────
        public static readonly DependencyProperty PlaceholderProperty =
            DependencyProperty.RegisterAttached(
                "Placeholder",
                typeof(string),
                typeof(MotionBehaviors),
                new PropertyMetadata(string.Empty));

        public static string GetPlaceholder(DependencyObject obj)
            => (string)obj.GetValue(PlaceholderProperty);

        public static void SetPlaceholder(DependencyObject obj, string value)
            => obj.SetValue(PlaceholderProperty, value);

        // ── CountUpText (smooth integer roll on KPI TextBlocks) ──────────────
        public static readonly DependencyProperty CountUpTextProperty =
            DependencyProperty.RegisterAttached(
                "CountUpText",
                typeof(string),
                typeof(MotionBehaviors),
                new PropertyMetadata(null, OnCountUpTextChanged));

        private static readonly DependencyProperty CountUpStateProperty =
            DependencyProperty.RegisterAttached(
                "CountUpState",
                typeof(CountUpState),
                typeof(MotionBehaviors),
                new PropertyMetadata(null));

        public static string? GetCountUpText(DependencyObject obj)
            => (string?)obj.GetValue(CountUpTextProperty);

        public static void SetCountUpText(DependencyObject obj, string? value)
            => obj.SetValue(CountUpTextProperty, value);

        private static void OnCountUpTextChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
        {
            if (d is not TextBlock textBlock) return;
            string nextText = e.NewValue as string ?? string.Empty;

            var state = (CountUpState?)textBlock.GetValue(CountUpStateProperty);
            if (state is null)
            {
                state = new CountUpState(textBlock);
                textBlock.SetValue(CountUpStateProperty, state);
            }

            state.Update(nextText);
        }

        private sealed class CountUpState
        {
            private readonly TextBlock target;
            private bool initialized;
            private long displayedValue;
            private long startValue;
            private long endValue;
            private Stopwatch? stopwatch;
            private EventHandler? renderingHandler;

            public CountUpState(TextBlock target)
            {
                this.target = target;
                target.Unloaded += (_, _) => Stop();
            }

            public void Update(string nextText)
            {
                if (!long.TryParse(nextText.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out long parsed))
                {
                    Stop();
                    initialized = false;
                    target.Text = nextText;
                    return;
                }

                if (!initialized || !target.IsLoaded || !SystemParameters.ClientAreaAnimation || displayedValue == parsed)
                {
                    Stop();
                    initialized = true;
                    displayedValue = parsed;
                    target.Text = nextText;
                    return;
                }

                startValue = displayedValue;
                endValue = parsed;
                Stop();
                stopwatch = Stopwatch.StartNew();

                EventHandler? handler = null;
                handler = (_, _) =>
                {
                    double elapsed = stopwatch?.Elapsed.TotalMilliseconds ?? CountUpDurationMs;
                    double t = Math.Min(1.0, elapsed / CountUpDurationMs);
                    double inv = 1.0 - t;
                    double eased = 1.0 - inv * inv * inv;
                    long current = t >= 1.0
                        ? endValue
                        : startValue + (long)Math.Round((endValue - startValue) * eased);

                    if (current != displayedValue || t >= 1.0)
                    {
                        displayedValue = current;
                        target.Text = current.ToString(CultureInfo.InvariantCulture);
                    }

                    if (t >= 1.0)
                    {
                        Stop();
                    }
                };

                renderingHandler = handler;
                CompositionTarget.Rendering += handler;
            }

            private void Stop()
            {
                if (renderingHandler is not null)
                {
                    CompositionTarget.Rendering -= renderingHandler;
                    renderingHandler = null;
                }
                stopwatch?.Stop();
                stopwatch = null;
            }
        }

        // ── EnableSpotlight (subtle radial hover sheen on cards/tiles) ───────
        public static readonly DependencyProperty EnableSpotlightProperty =
            DependencyProperty.RegisterAttached(
                "EnableSpotlight",
                typeof(bool),
                typeof(MotionBehaviors),
                new PropertyMetadata(false, OnEnableSpotlightChanged));

        private static readonly DependencyProperty SpotlightControllerProperty =
            DependencyProperty.RegisterAttached(
                "SpotlightController",
                typeof(SpotlightController),
                typeof(MotionBehaviors),
                new PropertyMetadata(null));

        public static bool GetEnableSpotlight(DependencyObject obj)
            => (bool)obj.GetValue(EnableSpotlightProperty);

        public static void SetEnableSpotlight(DependencyObject obj, bool value)
            => obj.SetValue(EnableSpotlightProperty, value);

        private static void OnEnableSpotlightChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
        {
            if (d is not Border overlay) return;

            var existing = (SpotlightController?)overlay.GetValue(SpotlightControllerProperty);
            existing?.Detach();
            overlay.ClearValue(SpotlightControllerProperty);

            if (e.NewValue is true)
            {
                var controller = new SpotlightController(overlay);
                overlay.SetValue(SpotlightControllerProperty, controller);
                controller.Attach();
            }
        }

        private sealed class SpotlightController
        {
            private readonly Border overlay;
            private FrameworkElement? host;
            private RadialGradientBrush? brush;
            private GradientStop? innerStop;

            public SpotlightController(Border overlay)
            {
                this.overlay = overlay;
            }

            public void Attach()
            {
                overlay.Loaded += OnLoaded;
                overlay.Unloaded += OnUnloaded;
                if (overlay.IsLoaded)
                {
                    BindHost();
                }
            }

            public void Detach()
            {
                overlay.Loaded -= OnLoaded;
                overlay.Unloaded -= OnUnloaded;
                UnbindHost();
            }

            private void OnLoaded(object sender, RoutedEventArgs e) => BindHost();

            private void OnUnloaded(object sender, RoutedEventArgs e) => UnbindHost();

            private void BindHost()
            {
                UnbindHost();
                host = overlay.TemplatedParent as FrameworkElement
                    ?? VisualTreeHelper.GetParent(overlay) as FrameworkElement
                    ?? overlay;
                if (host is null) return;

                host.MouseEnter += OnMouseEnter;
                host.MouseMove += OnMouseMove;
                host.MouseLeave += OnMouseLeave;
            }

            private void UnbindHost()
            {
                if (host is null) return;
                host.MouseEnter -= OnMouseEnter;
                host.MouseMove -= OnMouseMove;
                host.MouseLeave -= OnMouseLeave;
                host = null;
            }

            private void EnsureBrush()
            {
                Color accent = (overlay.TryFindResource("AccentIndicator") as SolidColorBrush)?.Color
                    ?? Color.FromRgb(124, 58, 237);
                Color glow = Color.FromArgb(0x22, accent.R, accent.G, accent.B);
                Color edge = Color.FromArgb(0x00, accent.R, accent.G, accent.B);

                if (brush is null || innerStop is null)
                {
                    innerStop = new GradientStop(glow, 0.0);
                    brush = new RadialGradientBrush
                    {
                        MappingMode = BrushMappingMode.RelativeToBoundingBox,
                        RadiusX = 0.62,
                        RadiusY = 1.05,
                        Center = new Point(0.5, 0.5),
                        GradientOrigin = new Point(0.5, 0.5),
                        GradientStops = new GradientStopCollection
                        {
                            innerStop,
                            new GradientStop(edge, 1.0),
                        },
                    };
                    overlay.Background = brush;
                }
                else
                {
                    innerStop.Color = glow;
                    if (brush.GradientStops.Count > 1)
                    {
                        brush.GradientStops[1].Color = edge;
                    }
                }
            }

            private void OnMouseEnter(object sender, MouseEventArgs e)
            {
                EnsureBrush();
                UpdateOrigin(e);
                AnimateOpacity(1.0, 110);
            }

            private void OnMouseMove(object sender, MouseEventArgs e)
            {
                if (brush is null) EnsureBrush();
                UpdateOrigin(e);
            }

            private void OnMouseLeave(object sender, MouseEventArgs e)
            {
                AnimateOpacity(0.0, 180);
            }

            private void UpdateOrigin(MouseEventArgs e)
            {
                if (host is null || brush is null || host.ActualWidth <= 0 || host.ActualHeight <= 0)
                    return;

                Point pos = e.GetPosition(host);
                double nx = Math.Clamp(pos.X / host.ActualWidth, 0.0, 1.0);
                double ny = Math.Clamp(pos.Y / host.ActualHeight, 0.0, 1.0);
                var pt = new Point(nx, ny);
                brush.Center = pt;
                brush.GradientOrigin = pt;
            }

            private void AnimateOpacity(double to, int durationMs)
            {
                if (!SystemParameters.ClientAreaAnimation)
                {
                    overlay.BeginAnimation(UIElement.OpacityProperty, null);
                    overlay.Opacity = to;
                    return;
                }

                var anim = new DoubleAnimation(to, TimeSpan.FromMilliseconds(durationMs))
                {
                    EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut },
                };
                overlay.BeginAnimation(UIElement.OpacityProperty, anim);
            }
        }

        // ── SoftPanelEntrance (subtle 4px fade-in helper for view switches) ──
        public static void PlaySoftEntrance(UIElement element, double offsetY = 4.0, int durationMs = 150)
        {
            if (element is null) return;
            if (!SystemParameters.ClientAreaAnimation)
            {
                element.Opacity = 1.0;
                return;
            }

            if (element.RenderTransform is not TranslateTransform translate || translate.IsFrozen)
            {
                translate = new TranslateTransform();
                element.RenderTransform = translate;
            }

            var ease = new CubicEase { EasingMode = EasingMode.EaseOut };
            var fade = new DoubleAnimation(0.0, 1.0, TimeSpan.FromMilliseconds(durationMs)) { EasingFunction = ease };
            var slide = new DoubleAnimation(offsetY, 0.0, TimeSpan.FromMilliseconds(durationMs)) { EasingFunction = ease };
            element.BeginAnimation(UIElement.OpacityProperty, fade);
            translate.BeginAnimation(TranslateTransform.YProperty, slide);
        }
    }
}
