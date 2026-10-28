// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System.Runtime.InteropServices;
using System.Windows.Input;
using System.Windows.Interop;

namespace SmsWorkbench
{
    /// <summary>One process-wide hotkey that brings the shell forward.</summary>
    public interface IGlobalHotkeyService : IDisposable
    {
        /// <summary>True when the OS accepted the registration.</summary>
        bool IsRegistered { get; }

        /// <summary>Raised on the UI thread when the combination is pressed.</summary>
        event EventHandler? Pressed;

        /// <summary>
        /// Registers the combination against <paramref name="window"/>'s handle.
        /// Returns false (never throws) when another process already owns it.
        /// </summary>
        bool TryRegister(Window window, ModifierKeys modifiers, Key key);
    }

    /// <summary>
    /// <c>RegisterHotKey</c> wrapper.
    /// </summary>
    /// <remarks>
    /// The hotkey goes to a window handle rather than a message-only window because
    /// the shell already has one and the WM_HOTKEY hook can ride on the same
    /// <c>HwndSource</c>. Registration is advisory: another application may own the
    /// combination, and the app must still start.
    /// </remarks>
    public sealed class GlobalHotkeyService : IGlobalHotkeyService
    {
        /// <summary>The combination the shell ships with.</summary>
        /// <remarks>
        /// A field, not a const: <see cref="ModifierKeys"/> is a flags enum, so the
        /// combination is a runtime value and cannot be folded into a literal.
        /// </remarks>
        public static readonly ModifierKeys DefaultModifiers = ModifierKeys.Control | ModifierKeys.Alt;

        /// <summary>Key half of the shipped combination.</summary>
        public const Key DefaultKey = Key.R;

        /// <summary>Human label, shared by the dialog and the log so they cannot disagree.</summary>
        public const string DefaultLabel = "Ctrl+Alt+R";

        private const int WmHotkey = 0x0312;
        private const int HotkeyId = 0x5A17;

        private const uint ModAlt = 0x0001;
        private const uint ModControl = 0x0002;
        private const uint ModShift = 0x0004;
        private const uint ModWin = 0x0008;
        private const uint ModNoRepeat = 0x4000;

        private HwndSource? source;
        private IntPtr handle;

        public bool IsRegistered { get; private set; }

        public event EventHandler? Pressed;

        public bool TryRegister(Window window, ModifierKeys modifiers, Key key)
        {
            if (IsRegistered || window is null) return IsRegistered;

            IntPtr windowHandle = new WindowInteropHelper(window).Handle;
            if (windowHandle == IntPtr.Zero) return false;

            uint flags = ToWin32Modifiers(modifiers) | ModNoRepeat;
            if (!RegisterHotKey(windowHandle, HotkeyId, flags, (uint)KeyInterop.VirtualKeyFromKey(key)))
                return false;

            handle = windowHandle;
            source = HwndSource.FromHwnd(windowHandle);
            source?.AddHook(WndProc);
            IsRegistered = true;
            return true;
        }

        public void Dispose()
        {
            if (source is not null)
            {
                source.RemoveHook(WndProc);
                source = null;
            }

            if (IsRegistered && handle != IntPtr.Zero)
            {
                UnregisterHotKey(handle, HotkeyId);
            }

            IsRegistered = false;
            handle = IntPtr.Zero;
        }

        private IntPtr WndProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
        {
            // wParam is the id RegisterHotKey was given; a bare WM_HOTKEY for another
            // registration would otherwise steal the keystroke.
            if (msg == WmHotkey && wParam.ToInt32() == HotkeyId)
            {
                handled = true;
                Pressed?.Invoke(this, EventArgs.Empty);
            }

            return IntPtr.Zero;
        }

        private static uint ToWin32Modifiers(ModifierKeys modifiers)
        {
            uint flags = 0;
            if ((modifiers & ModifierKeys.Alt) != 0) flags |= ModAlt;
            if ((modifiers & ModifierKeys.Control) != 0) flags |= ModControl;
            if ((modifiers & ModifierKeys.Shift) != 0) flags |= ModShift;
            if ((modifiers & ModifierKeys.Windows) != 0) flags |= ModWin;
            return flags;
        }

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool RegisterHotKey(IntPtr hWnd, int id, uint fsModifiers, uint vk);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool UnregisterHotKey(IntPtr hWnd, int id);
    }
}
