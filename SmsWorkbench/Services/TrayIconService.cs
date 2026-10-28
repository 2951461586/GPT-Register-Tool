// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System.Runtime.InteropServices;
using System.Windows.Interop;

namespace SmsWorkbench
{
    /// <summary>The shell's notification-area icon and its two menu actions.</summary>
    public interface ITrayIconService : IDisposable
    {
        /// <summary>
        /// True once the icon is actually in the notification area. False in a session
        /// with no shell (CI, a service account) and after a failed registration;
        /// callers use it to decide whether hiding the window is recoverable.
        /// </summary>
        bool IsAvailable { get; }

        /// <summary>True once "quit" was chosen from the tray menu.</summary>
        bool ExitRequested { get; }

        /// <summary>Raised when the operator asks for the window back.</summary>
        event EventHandler? ShowRequested;

        /// <summary>Raised after <see cref="ExitRequested"/> flips to true.</summary>
        event EventHandler? QuitRequested;

        /// <summary>Creates the icon. Safe to call twice; never throws.</summary>
        void Initialize();

        /// <summary>Shows a one-off balloon, if the session allows one.</summary>
        void NotifyHiddenToTray();
    }

    /// <summary>
    /// <c>Shell_NotifyIcon</c> behind an interface.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Deliberately not WinForms' <c>NotifyIcon</c>. That would add the whole WinForms
    /// framework to a WPF process for one 16x16 icon, and its message plumbing is a
    /// second UI stack the app never otherwise touches. This is the same shape as
    /// <see cref="GlobalHotkeyService"/> and <see cref="WindowThemeService"/>: an
    /// <c>HwndSource</c> hook plus the shell API.
    /// </para>
    /// <para>
    /// The window is a hidden top-level tool window rather than a message-only window:
    /// message-only windows do not receive the <c>TaskbarCreated</c> broadcast, and the
    /// icon has to be re-added when Explorer restarts.
    /// </para>
    /// <para>
    /// Every failure is swallowed on purpose. A session without an interactive shell
    /// fails at icon creation, and that must degrade to "no tray" — the window then
    /// closes normally (<see cref="CloseToTrayPolicy"/>) instead of hiding forever.
    /// </para>
    /// </remarks>
    public sealed class TrayIconService : ITrayIconService
    {
        private const int WmApp = 0x8000;
        private const int WmTray = WmApp + 1;
        private const int WmLButtonUp = 0x0202;
        private const int WmLButtonDoubleClick = 0x0203;
        private const int WmRButtonUp = 0x0205;
        private const int WmContextMenu = 0x007B;
        private const int WmNull = 0x0000;

        private const int WsExToolWindow = 0x00000080;

        private const uint NimAdd = 0x00000000;
        private const uint NimModify = 0x00000001;
        private const uint NimDelete = 0x00000002;

        private const uint NifMessage = 0x00000001;
        private const uint NifIcon = 0x00000002;
        private const uint NifTip = 0x00000004;
        private const uint NifInfo = 0x00000010;

        private const uint MfString = 0x00000000;
        private const uint MfSeparator = 0x00000800;
        private const uint TpmReturnCommand = 0x0100;
        private const uint TpmRightButton = 0x0002;

        private const uint ShowCommand = 1;
        private const uint QuitCommand = 2;


        private HwndSource? messageSource;
        private IntPtr iconHandle;
        private bool ownsIcon;
        private bool added;
        private bool notifiedHidden;
        private int taskbarCreatedMessage;

        public bool IsAvailable => added;

        public bool ExitRequested { get; private set; }

        public event EventHandler? ShowRequested;

        public event EventHandler? QuitRequested;

        public void Initialize()
        {
            if (added) return;

            try
            {
                var parameters = new HwndSourceParameters("SmsWorkbenchTrayWindow")
                {
                    // ExToolWindow keeps the hidden window out of Alt-Tab and the shell's
                    // window list; a plain hidden top-level window would still be a
                    // keyboard-navigable window.
                    ExtendedWindowStyle = WsExToolWindow,
                    WindowStyle = 0,
                };

                messageSource = new HwndSource(parameters);
                messageSource.AddHook(WndProc);
                taskbarCreatedMessage = RegisterWindowMessage("TaskbarCreated");

                iconHandle = LoadAppIcon();
                AddIcon(NifMessage | NifIcon | NifTip);
                added = true;
            }
            catch (Exception)
            {
                // No shell to register with. Not a failure worth a dialog: the window
                // keeps its normal close behaviour (CloseToTrayPolicy returns false).
                DisposeNative();
            }
        }

        private IntPtr WndProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
        {
            // Explorer restarted: the icon is gone with it.
            if (taskbarCreatedMessage != 0 && msg == taskbarCreatedMessage)
            {
                added = false;
                Initialize();
                handled = true;
                return IntPtr.Zero;
            }

            if (msg != WmTray) return IntPtr.Zero;

            int mouseMessage = lParam.ToInt32() & 0xFFFF;
            if (mouseMessage is WmLButtonUp or WmLButtonDoubleClick)
            {
                handled = true;
                ShowRequested?.Invoke(this, EventArgs.Empty);
            }
            else if (mouseMessage is WmRButtonUp or WmContextMenu)
            {
                handled = true;
                ShowMenu();
            }

            return IntPtr.Zero;
        }

        private void ShowMenu()
        {
            if (messageSource is null) return;
            IntPtr menu = IntPtr.Zero;
            try
            {
                menu = CreatePopupMenu();
                if (menu == IntPtr.Zero) return;
                AppendMenuW(menu, MfString, ShowCommand, "显示主窗口");
                AppendMenuW(menu, MfSeparator, 0, null);
                AppendMenuW(menu, MfString, QuitCommand, "退出");
                GetCursorPos(out POINT point);

                // The documented dance for a tray menu: without foreground ownership the
                // popup stays on screen after the click, and the WM_NULL afterwards
                // releases the modal loop TrackPopupMenu runs.
                SetForegroundWindow(messageSource.Handle);
                uint command = TrackPopupMenuEx(
                    menu,
                    TpmReturnCommand | TpmRightButton,
                    point.X,
                    point.Y,
                    messageSource.Handle,
                    IntPtr.Zero);
                PostMessageW(messageSource.Handle, WmNull, IntPtr.Zero, IntPtr.Zero);

                if (command == ShowCommand)
                {
                    ShowRequested?.Invoke(this, EventArgs.Empty);
                }
                else if (command == QuitCommand)
                {
                    ExitRequested = true;
                    QuitRequested?.Invoke(this, EventArgs.Empty);
                }
            }
            catch (Exception)
            {
                // A menu that cannot be shown must not take the shell down with it.
            }
            finally
            {
                if (menu != IntPtr.Zero) DestroyMenu(menu);
            }
        }

        public void NotifyHiddenToTray()
        {
            if (notifiedHidden || !added) return;
            notifiedHidden = true;
            try
            {
                AddIcon(NifInfo, balloon: true);
            }
            catch (Exception)
            {
                // Balloons need the same shell the icon does; a missing one is fine.
            }
        }

        private void AddIcon(uint flags, bool balloon = false)
        {
            if (messageSource is null) return;
            var data = new NotifyIconData
            {
                cbSize = Marshal.SizeOf<NotifyIconData>(),
                hWnd = messageSource.Handle,
                uID = 1,
                uFlags = flags,
                uCallbackMessage = WmTray,
                hIcon = iconHandle,
                szTip = "GPT Register Tool",
                szInfo = balloon
                    ? "批量任务没有中断。双击托盘图标或 Ctrl+Alt+R 可回到窗口。"
                    : "",
                szInfoTitle = balloon ? "仍在后台运行" : "",
            };
            bool modify = added;
            Shell_NotifyIcon(modify ? NimModify : NimAdd, ref data);
        }

        public void Dispose()
        {
            if (added && messageSource is not null)
            {
                var data = new NotifyIconData
                {
                    cbSize = Marshal.SizeOf<NotifyIconData>(),
                    hWnd = messageSource.Handle,
                    uID = 1,
                };
                Shell_NotifyIcon(NimDelete, ref data);
            }

            DisposeNative();
        }

        private void DisposeNative()
        {
            if (messageSource is not null)
            {
                messageSource.RemoveHook(WndProc);
                messageSource.Dispose();
                messageSource = null;
            }

            if (ownsIcon && iconHandle != IntPtr.Zero) DestroyIcon(iconHandle);
            iconHandle = IntPtr.Zero;
            ownsIcon = false;
            added = false;
        }

        /// <summary>
        /// The application icon, taken from the executable the build already stamped it
        /// into. <c>ExtractIconEx</c> gives an icon this process must destroy, which is
        /// why ownership is tracked; the shared fallback must not be destroyed.
        /// </summary>
        private static IntPtr LoadAppIcon()
        {
            string executable = Environment.ProcessPath ?? "";
            if (executable.Length > 0)
            {
                IntPtr large = IntPtr.Zero;
                if (ExtractIconEx(executable, 0, out large, out IntPtr small, 1) > 0)
                {
                    if (large != IntPtr.Zero) DestroyIcon(large);
                    if (small != IntPtr.Zero) return small;
                }
            }

            return LoadIcon(IntPtr.Zero, (IntPtr)32512);
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct POINT
        {
            public int X;
            public int Y;
        }

        /// <summary>
        /// <c>NOTIFYICONDATAW</c>, Vista-and-later layout.
        /// </summary>
        /// <remarks>
        /// The shell validates <c>cbSize</c> against the layouts it knows, so every
        /// field of the version being used has to be present even when it is empty -
        /// a truncated struct is rejected and the icon silently never appears.
        /// </remarks>
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct NotifyIconData
        {
            public int cbSize;
            public IntPtr hWnd;
            public uint uID;
            public uint uFlags;
            public uint uCallbackMessage;
            public IntPtr hIcon;

            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)]
            public string szTip;

            public uint dwState;
            public uint dwStateMask;

            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
            public string szInfo;

            public uint uVersion;

            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)]
            public string szInfoTitle;

            public uint dwInfoFlags;
            public Guid guidItem;
            public IntPtr hBalloonIcon;
        }

        [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool Shell_NotifyIcon(uint message, ref NotifyIconData data);

        [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint ExtractIconEx(string file, int index, out IntPtr large, out IntPtr small, uint count);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern IntPtr LoadIcon(IntPtr instance, IntPtr iconName);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool DestroyIcon(IntPtr icon);

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreatePopupMenu();

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool AppendMenuW(IntPtr menu, uint flags, uint itemId, string? item);

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern uint TrackPopupMenuEx(
            IntPtr menu,
            uint flags,
            int x,
            int y,
            IntPtr window,
            IntPtr parameters);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool DestroyMenu(IntPtr menu);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetCursorPos(out POINT point);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetForegroundWindow(IntPtr window);

        [DllImport("user32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool PostMessageW(IntPtr window, int message, IntPtr wParam, IntPtr lParam);

        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern int RegisterWindowMessage(string message);
    }
}
