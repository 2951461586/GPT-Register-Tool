using System.Runtime.InteropServices;
using System.Windows.Interop;
using System.Windows.Media;

namespace SmsWorkbench.Services;

internal static class WindowThemeService
{
    private const int DwmwaUseImmersiveDarkMode = 20;
    private const int DwmwaCaptionColor = 35;
    private const int DwmwaTextColor = 36;

    public static void Attach(Window window)
    {
        if (window == null) return;
        ApplyTypography(window);
        window.Closing += OnChildWindowClosing;

        if (new WindowInteropHelper(window).Handle != IntPtr.Zero)
        {
            Apply(window);
            return;
        }

        void OnSourceInitialized(object? sender, EventArgs e)
        {
            window.SourceInitialized -= OnSourceInitialized;
            Apply(window);
        }

        window.SourceInitialized += OnSourceInitialized;
    }

    private static void ApplyTypography(Window window)
    {
        window.SetResourceReference(Control.FontFamilyProperty, "Font.Ui");
        window.SetResourceReference(Control.FontSizeProperty, "Type.Body.Size");
        window.UseLayoutRounding = true;
        window.SnapsToDevicePixels = true;
        TextOptions.SetTextFormattingMode(window, TextFormattingMode.Ideal);
        TextOptions.SetTextRenderingMode(window, TextRenderingMode.ClearType);
        TextOptions.SetTextHintingMode(window, TextHintingMode.Fixed);
    }

    private static void OnChildWindowClosing(object? sender, CancelEventArgs e)
    {
        // Win32 destroys an owned child window before WPF hands activation back,
        // which can push the owner behind other desktop windows (most noticeably
        // on non-modal .Show() windows like InboxDialogWindow). Re-activating the
        // owner during Closing keeps it in the foreground cleanly.
        if (e.Cancel || sender is not Window window) return;
        if (window.Owner is { IsVisible: true } owner && owner.WindowState != WindowState.Minimized)
        {
            owner.Activate();
        }
    }

    public static void Apply(Window window)
    {
        if (window == null) return;
        try
        {
            var helper = new WindowInteropHelper(window);
            if (helper.Handle == IntPtr.Zero) return;

            bool dark = IsDarkTheme();
            SetAttribute(helper.Handle, DwmwaUseImmersiveDarkMode, dark ? 1 : 0);
            SetAttribute(helper.Handle, DwmwaCaptionColor, ToColorRef(FindColor("PanelBg")));
            SetAttribute(helper.Handle, DwmwaTextColor, ToColorRef(FindColor("TextMain")));
        }
        catch
        {
            // Older Windows versions may not expose the DWM attributes.
        }
    }

    public static void ApplyToOpenWindows()
    {
        foreach (Window window in Application.Current.Windows)
            Apply(window);
    }

    private static bool IsDarkTheme()
    {
        Color panel = FindColor("PanelBg");
        return panel.R < 80 && panel.G < 80 && panel.B < 80;
    }

    private static Color FindColor(string key)
    {
        return (Application.Current.Resources[key] as SolidColorBrush)?.Color ?? Colors.Black;
    }

    private static int ToColorRef(Color color) => color.R | (color.G << 8) | (color.B << 16);

    private static void SetAttribute(IntPtr handle, int attribute, int value)
    {
        int result = DwmSetWindowAttribute(handle, attribute, ref value, sizeof(int));
        if (result != 0) return;
    }

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int valueSize);
}
