using System.Globalization;
using SmsWorkbench;
using Xunit;

namespace SmsWorkbench.Tests;

public class RegistrationStatusPresentationTests
{
    [Theory]
    [InlineData("partial_registered")]
    [InlineData("PARTIAL_REGISTERED")]
    [InlineData("半注册")]
    public void PartialRegistrationIsNotAnUnusedMailbox(string state)
    {
        Assert.True(RegistrationStatusPresentation.IsPartial(state));
        Assert.Equal("半注册", RegistrationStatusPresentation.MailboxStatus(state, "可收信"));
        Assert.True(RegistrationStatusPresentation.NeedsAttention(new PoolRow { RegistrationStatus = state }));
    }

    [Fact]
    public void OrdinaryMailboxAndRegisteredAccountKeepTheirStatus()
    {
        Assert.Equal("可收信", RegistrationStatusPresentation.MailboxStatus("unknown", "可收信"));
        Assert.False(RegistrationStatusPresentation.IsPartial("registered"));
        Assert.Equal("已注册", AccountStatusInterpreter.DisplayAccountStatus("registered", "", "AT", "", "", "", ""));
    }

    [Fact]
    public void PartialRegistrationHasWarningSeverityAndExplicitAccountLabel()
    {
        Assert.Equal("warn", new StatusSeverityConverter().Convert("半注册", typeof(string), null!, CultureInfo.InvariantCulture));
        Assert.Equal("半注册", AccountStatusInterpreter.DisplayAccountStatus("partial_registered", "", "", "user_already_exists", "", "", ""));
    }

    [Fact]
    public void PartialRegistrationEventHasAnOperatorLine()
    {
        var progress = new BackendProgressEvent("registration", "run", "abc123", "", "registration_status_changed", "running", "半注册");
        Assert.Equal("注册 · abc123 · 半注册", BackendLogPresenter.ProgressEventLine(progress));
    }
}
