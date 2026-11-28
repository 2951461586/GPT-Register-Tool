using System.Windows.Media.Animation;
using System.Windows.Shapes;

namespace SmsWorkbench.Views
{
    public partial class WorkflowStepper : UserControl
    {
        public static readonly DependencyProperty Step1LabelProperty = DependencyProperty.Register(
            nameof(Step1Label), typeof(string), typeof(WorkflowStepper), new PropertyMetadata(string.Empty));

        public static readonly DependencyProperty Step2LabelProperty = DependencyProperty.Register(
            nameof(Step2Label), typeof(string), typeof(WorkflowStepper), new PropertyMetadata(string.Empty));

        public static readonly DependencyProperty Step3LabelProperty = DependencyProperty.Register(
            nameof(Step3Label), typeof(string), typeof(WorkflowStepper), new PropertyMetadata(string.Empty));

        public static readonly DependencyProperty ActiveStepIndexProperty = DependencyProperty.Register(
            nameof(ActiveStepIndex), typeof(int), typeof(WorkflowStepper),
            new PropertyMetadata(0, OnStepVisualChanged));

        public static readonly DependencyProperty StepStateProperty = DependencyProperty.Register(
            nameof(StepState), typeof(string), typeof(WorkflowStepper),
            new PropertyMetadata("idle", OnStepVisualChanged));

        private readonly Border[] dots;
        private readonly TextBlock[] nums;
        private readonly System.Windows.Shapes.Path[] checks;
        private readonly TextBlock[] labels;
        private readonly Border track1Fill;
        private readonly Border track2Fill;

        public WorkflowStepper()
        {
            InitializeComponent();
            dots = new[] { (Border)FindName("Step1Dot")!, (Border)FindName("Step2Dot")!, (Border)FindName("Step3Dot")! };
            nums = new[] { (TextBlock)FindName("Step1Num")!, (TextBlock)FindName("Step2Num")!, (TextBlock)FindName("Step3Num")! };
            checks = new[]
            {
                (System.Windows.Shapes.Path)FindName("Step1Check")!,
                (System.Windows.Shapes.Path)FindName("Step2Check")!,
                (System.Windows.Shapes.Path)FindName("Step3Check")!,
            };
            labels = new[] { (TextBlock)FindName("Step1Text")!, (TextBlock)FindName("Step2Text")!, (TextBlock)FindName("Step3Text")! };
            track1Fill = (Border)FindName("Track1Fill")!;
            track2Fill = (Border)FindName("Track2Fill")!;

            Loaded += (_, _) => ApplyVisualState(animate: false);
            SizeChanged += (_, _) => ApplyVisualState(animate: false);
        }

        public string Step1Label { get => (string)GetValue(Step1LabelProperty); set => SetValue(Step1LabelProperty, value); }
        public string Step2Label { get => (string)GetValue(Step2LabelProperty); set => SetValue(Step2LabelProperty, value); }
        public string Step3Label { get => (string)GetValue(Step3LabelProperty); set => SetValue(Step3LabelProperty, value); }
        public int ActiveStepIndex { get => (int)GetValue(ActiveStepIndexProperty); set => SetValue(ActiveStepIndexProperty, value); }
        public string StepState { get => (string)GetValue(StepStateProperty); set => SetValue(StepStateProperty, value); }

        private static void OnStepVisualChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
        {
            if (d is WorkflowStepper stepper)
            {
                stepper.ApplyVisualState(animate: stepper.IsLoaded);
            }
        }

        private void ApplyVisualState(bool animate)
        {
            int active = Math.Clamp(ActiveStepIndex, 0, 3);
            string state = (StepState ?? "idle").Trim().ToLowerInvariant();

            for (int i = 0; i < 3; i++)
            {
                bool completed = i < active;
                bool current = i == active;

                if (completed)
                {
                    dots[i].SetResourceReference(Border.BackgroundProperty, "Primary");
                    dots[i].SetResourceReference(Border.BorderBrushProperty, "Primary");
                    nums[i].Visibility = Visibility.Collapsed;
                    checks[i].Visibility = Visibility.Visible;
                    labels[i].SetResourceReference(TextBlock.ForegroundProperty, "TextMain");
                    labels[i].FontWeight = FontWeights.Medium;
                }
                else if (current)
                {
                    string accentKey = state switch
                    {
                        "warn" => "BadgeWarnFg",
                        "danger" => "Danger",
                        _ => "AccentIndicator",
                    };
                    string softKey = state switch
                    {
                        "warn" => "BadgeWarnBg",
                        "danger" => "DangerSoft",
                        _ => "PrimarySoft",
                    };
                    dots[i].SetResourceReference(Border.BackgroundProperty, softKey);
                    dots[i].SetResourceReference(Border.BorderBrushProperty, accentKey);
                    nums[i].SetResourceReference(TextBlock.ForegroundProperty, accentKey);
                    nums[i].Visibility = Visibility.Visible;
                    checks[i].Visibility = Visibility.Collapsed;
                    labels[i].SetResourceReference(TextBlock.ForegroundProperty, "TextMain");
                    labels[i].FontWeight = FontWeights.SemiBold;
                }
                else
                {
                    dots[i].SetResourceReference(Border.BackgroundProperty, "Surface.Block");
                    dots[i].SetResourceReference(Border.BorderBrushProperty, "Line");
                    nums[i].SetResourceReference(TextBlock.ForegroundProperty, "TextMuted");
                    nums[i].Visibility = Visibility.Visible;
                    checks[i].Visibility = Visibility.Collapsed;
                    labels[i].SetResourceReference(TextBlock.ForegroundProperty, "TextMuted");
                    labels[i].FontWeight = FontWeights.Normal;
                }
            }

            UpdateTrack(track1Fill, filled: active >= 1, animate);
            UpdateTrack(track2Fill, filled: active >= 2, animate);
        }

        private static void UpdateTrack(Border fill, bool filled, bool animate)
        {
            if (fill.Parent is not FrameworkElement host || host.ActualWidth <= 0)
            {
                return;
            }

            double targetWidth = filled ? host.ActualWidth : 0.0;
            if (!animate || !SystemParameters.ClientAreaAnimation)
            {
                fill.BeginAnimation(FrameworkElement.WidthProperty, null);
                fill.Width = targetWidth;
                return;
            }

            var anim = new DoubleAnimation(targetWidth, TimeSpan.FromMilliseconds(220))
            {
                EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut },
            };
            fill.BeginAnimation(FrameworkElement.WidthProperty, anim);
        }
    }
}
