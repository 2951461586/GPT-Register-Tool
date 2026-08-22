namespace SmsWorkbench
{
    public partial class MainWindow
    {
        private const double SidebarExpandedWidth = 248;
        private const double SidebarCollapsedWidth = 90;
        private const int SidebarAnimDurationMs = 100;

        private void ToggleSidebar_Click(object sender, RoutedEventArgs e)
        {
            SidebarCollapsed = !SidebarCollapsed;
        }

        private void ApplySidebarCompact(bool compact)
        {
            if (SidebarToggleButton != null)
            {
                SidebarToggleButton.ToolTip = compact ? "展开侧边栏" : "收起侧边栏";
            }

            SidebarToggleGlyph = compact ? "展开" : "收起";
            SidebarToggleGeometry = Geometry.Parse(compact
                ? "M5 4H19A1 1 0 0 1 20 5V19A1 1 0 0 1 19 20H5A1 1 0 0 1 4 19V5A1 1 0 0 1 5 4Z M14 4V20"
                : "M5 4H19A1 1 0 0 1 20 5V19A1 1 0 0 1 19 20H5A1 1 0 0 1 4 19V5A1 1 0 0 1 5 4Z M10 4V20");

            AnimateSidebar(compact);
        }

        private void AnimateSidebar(bool collapse)
        {
            double target = collapse ? SidebarCollapsedWidth : SidebarExpandedWidth;
            double current = SidebarColumn?.Width.Value ?? (collapse ? SidebarExpandedWidth : SidebarCollapsedWidth);

            sidebarAnimStart = current;
            sidebarAnimTarget = target;

            if (sidebarRenderingHandler != null)
            {
                CompositionTarget.Rendering -= sidebarRenderingHandler;
                sidebarRenderingHandler = null;
            }
            sidebarAnimStopwatch = Stopwatch.StartNew();

            double lastWidth = double.NaN;
            EventHandler? renderingHandler = null;
            renderingHandler = (_, _) =>
            {
                double elapsed = sidebarAnimStopwatch?.Elapsed.TotalMilliseconds ?? SidebarAnimDurationMs;
                double t = Math.Min(1.0, elapsed / SidebarAnimDurationMs);
                double inverse = 1 - t;
                double eased = 1 - inverse * inverse * inverse;
                double value = sidebarAnimStart + (sidebarAnimTarget - sidebarAnimStart) * eased;

                if (SidebarColumn != null && (double.IsNaN(lastWidth) || Math.Abs(value - lastWidth) >= 0.1 || t >= 1.0))
                {
                    SidebarColumn.Width = new GridLength(value);
                    lastWidth = value;
                }

                if (t < 1.0 || renderingHandler == null)
                {
                    return;
                }

                CompositionTarget.Rendering -= renderingHandler;
                if (ReferenceEquals(sidebarRenderingHandler, renderingHandler))
                {
                    sidebarRenderingHandler = null;
                }
                sidebarAnimStopwatch?.Stop();
                sidebarAnimStopwatch = null;
            };

            sidebarRenderingHandler = renderingHandler;
            CompositionTarget.Rendering += renderingHandler;
        }
    }
}
