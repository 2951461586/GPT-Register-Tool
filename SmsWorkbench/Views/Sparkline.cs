namespace SmsWorkbench.Views
{
    /// <summary>
    /// A one-line trend of a value series, drawn directly (no charting dependency).
    /// </summary>
    /// <remarks>
    /// <para>
    /// The shell needs exactly one kind of chart - "is throughput rising or falling" -
    /// and a charting dependency for that would be a lifetime of theme integration for
    /// one polyline. The control normalises the series to its own height, so callers
    /// pass counts, not pixels.
    /// </para>
    /// <para>
    /// It redraws from <see cref="Values"/> only: the view model hands out a fresh list
    /// on every sample, and an <c>AffectsRender</c> property change is what triggers
    /// <see cref="OnRender"/>.
    /// </para>
    /// </remarks>
    public sealed class Sparkline : FrameworkElement
    {
        public static readonly DependencyProperty ValuesProperty = DependencyProperty.Register(
            nameof(Values),
            typeof(IEnumerable<double>),
            typeof(Sparkline),
            new FrameworkPropertyMetadata(null, FrameworkPropertyMetadataOptions.AffectsRender));

        public static readonly DependencyProperty StrokeProperty = DependencyProperty.Register(
            nameof(Stroke),
            typeof(Brush),
            typeof(Sparkline),
            new FrameworkPropertyMetadata(Brushes.Gray, FrameworkPropertyMetadataOptions.AffectsRender));

        public static readonly DependencyProperty StrokeThicknessProperty = DependencyProperty.Register(
            nameof(StrokeThickness),
            typeof(double),
            typeof(Sparkline),
            new FrameworkPropertyMetadata(1.2, FrameworkPropertyMetadataOptions.AffectsRender));

        public IEnumerable<double>? Values
        {
            get => (IEnumerable<double>?)GetValue(ValuesProperty);
            set => SetValue(ValuesProperty, value);
        }

        public Brush Stroke
        {
            get => (Brush)GetValue(StrokeProperty);
            set => SetValue(StrokeProperty, value);
        }

        public double StrokeThickness
        {
            get => (double)GetValue(StrokeThicknessProperty);
            set => SetValue(StrokeThicknessProperty, value);
        }

        protected override void OnRender(DrawingContext drawingContext)
        {
            base.OnRender(drawingContext);

            double[]? values = Values?.ToArray();
            double width = ActualWidth;
            double height = ActualHeight;
            // Two points make a line; one point or a zero-sized layout is a no-op
            // instead of a divide-by-zero inside the renderer.
            if (values is null || values.Length < 2 || width <= 0 || height <= 0) return;

            double max = values.Max();
            if (max <= 0)
            {
                Brush muted = TryFindResource("Divider") as Brush ?? Brushes.Gray;
                var quietPen = new Pen(muted, 1.0) { DashStyle = DashStyles.Dot };
                quietPen.Freeze();
                double midY = Math.Round(height * 0.5) + 0.5;
                drawingContext.DrawLine(quietPen, new Point(0, midY), new Point(width, midY));
                return;
            }

            const double padY = 1.5;
            double plotHeight = Math.Max(1.0, height - padY * 2.0);
            double step = width / (values.Length - 1);

            Point PointAt(int i) => new(i * step, padY + (1.0 - values[i] / max) * plotHeight);

            var strokeGeometry = new StreamGeometry();
            var areaGeometry = new StreamGeometry();
            Point first = PointAt(0);
            Point last = PointAt(values.Length - 1);

            using (StreamGeometryContext strokeCtx = strokeGeometry.Open())
            using (StreamGeometryContext areaCtx = areaGeometry.Open())
            {
                strokeCtx.BeginFigure(first, isFilled: false, isClosed: false);
                areaCtx.BeginFigure(new Point(0, height), isFilled: true, isClosed: true);
                areaCtx.LineTo(first, isStroked: false, isSmoothJoin: false);

                for (int index = 1; index < values.Length; index++)
                {
                    Point pt = PointAt(index);
                    strokeCtx.LineTo(pt, isStroked: true, isSmoothJoin: true);
                    areaCtx.LineTo(pt, isStroked: false, isSmoothJoin: false);
                }

                areaCtx.LineTo(new Point(width, height), isStroked: false, isSmoothJoin: false);
            }

            strokeGeometry.Freeze();
            areaGeometry.Freeze();

            Color strokeColor = (Stroke as SolidColorBrush)?.Color ?? Colors.Gray;
            var fillBrush = new LinearGradientBrush(
                Color.FromArgb(0x36, strokeColor.R, strokeColor.G, strokeColor.B),
                Color.FromArgb(0x00, strokeColor.R, strokeColor.G, strokeColor.B),
                new Point(0, 0),
                new Point(0, 1));
            fillBrush.Freeze();
            drawingContext.DrawGeometry(fillBrush, null, areaGeometry);

            var pen = new Pen(Stroke, StrokeThickness)
            {
                LineJoin = PenLineJoin.Round,
                StartLineCap = PenLineCap.Round,
                EndLineCap = PenLineCap.Round,
            };
            pen.Freeze();
            drawingContext.DrawGeometry(null, pen, strokeGeometry);

            if (values[^1] > 0)
            {
                var halo = new SolidColorBrush(Color.FromArgb(0x38, strokeColor.R, strokeColor.G, strokeColor.B));
                halo.Freeze();
                drawingContext.DrawEllipse(halo, null, last, 3.6, 3.6);
                drawingContext.DrawEllipse(Stroke, null, last, 1.8, 1.8);
            }
        }
    }
}
