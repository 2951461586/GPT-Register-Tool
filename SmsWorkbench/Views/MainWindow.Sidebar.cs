// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        private const double SidebarExpandedWidth = 248;
        private const double SidebarCollapsedWidth = 90;
        private const int SidebarAnimDurationMs = 100;

        private void OnSidebarToggleRequested(object sender, RoutedEventArgs e)
        {
            SidebarCollapsed = !SidebarCollapsed;
        }

        private void ApplySidebarCompact(bool compact)
        {
            // Visual state (tooltip, glyph) lives in the Sidebar control's
            // triggers; the window only owns the column width animation.
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
