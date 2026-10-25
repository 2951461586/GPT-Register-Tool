namespace SmsWorkbench.Tests;

/// <summary>
/// The settings dialog edits only the legacy proxy keys, while
/// <c>proxy.lanes.&lt;lane&gt;</c> is authoritative in the backend when declared.
/// The notice is what tells the operator that, so its two states are pinned here
/// instead of being checked by reading a window.
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
