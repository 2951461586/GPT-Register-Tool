namespace SmsWorkbench.Tests;

/// <summary>
/// The settings dialog now edits the canonical <c>proxy.lanes.&lt;lane&gt;</c>
/// entries directly (and mirrors the legacy keys). The notice names the lanes it
/// owns, so its two states are pinned here instead of being checked by reading a
/// window.
/// </summary>
public sealed class SettingsViewModelTests
{
    [Fact]
    public void ProxyLaneNotice_IsEmptyWhenNoCanonicalLaneIsDeclared()
    {
        Assert.Equal("", SettingsViewModel.BuildProxyLaneNotice(Array.Empty<string>()));
    }

    [Fact]
    public void ProxyLaneNotice_NamesEveryDeclaredLaneAndTheAuthoritativeKey()
    {
        string notice = SettingsViewModel.BuildProxyLaneNotice(
            new[] { "protocol_registration", "mailbox" });

        Assert.NotEqual("", notice);
        // Both the Chinese source and the English translation keep the config key
        // verbatim, so this assertion does not depend on the active language.
        Assert.Contains("proxy.lanes", notice, StringComparison.Ordinal);
        Assert.Contains("protocol_registration", notice, StringComparison.Ordinal);
        Assert.Contains("mailbox", notice, StringComparison.Ordinal);
    }
}
