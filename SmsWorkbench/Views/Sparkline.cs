// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
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
            if (max <= 0) max = 1;

            var geometry = new StreamGeometry();
            using (StreamGeometryContext context = geometry.Open())
            {
                double step = width / (values.Length - 1);
                context.BeginFigure(new Point(0, height - values[0] / max * height), isFilled: false, isClosed: false);
                for (int index = 1; index < values.Length; index++)
                {
                    context.LineTo(new Point(index * step, height - values[index] / max * height), true, false);
                }
            }

            geometry.Freeze();
            var pen = new Pen(Stroke, StrokeThickness) { LineJoin = PenLineJoin.Round };
            pen.Freeze();
            drawingContext.DrawGeometry(null, pen, geometry);
        }
    }
}
