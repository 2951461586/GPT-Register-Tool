using SmsWorkbench;

namespace SmsWorkbench.Tests;

/// <summary>
/// The close-to-tray truth table on its own.
/// </summary>
/// <remarks>
/// <c>MainWindow.OnWindowClosing</c> cannot be exercised for the "hide" branch without
/// also exercising <c>Hide</c>, so the decision lives in
/// <see cref="CloseToTrayPolicy"/> and is pinned here; the window-level behaviour is
/// covered by the smoke test.
/// </remarks>
public sealed class TrayClosePolicyTests
{
    [Theory]
    [InlineData(true, false, true)]   // tray up, no quit request -> hide
    [InlineData(true, true, false)]   // quit from the tray menu -> close
    [InlineData(false, false, false)] // no tray -> close: hiding would have no way back
    [InlineData(false, true, false)]
    public void HideOnlyWhenTheTrayCanBringItBack(bool trayAvailable, bool exitRequested, bool expected)
        => Assert.Equal(expected, CloseToTrayPolicy.ShouldHideToTray(trayAvailable, exitRequested));
}
