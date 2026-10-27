using SmsWorkbench.Localization;

namespace SmsWorkbench.Tests;

/// <summary>
/// The grid's status column is <em>display</em> text: <see cref="Loc.T"/> returns
/// the English label once the operator switches language. Every predicate that
/// decides something about a row (is it registered? does it need attention? can
/// it be exported?) used to compare that text against the Chinese source
/// literal, so switching language silently changed behaviour: the registered
/// counter dropped, "needs attention" emptied, and already-registered mailboxes
/// were offered for registration again.
///
/// The predicates now match the marker in <b>any</b> shipped language, whatever
/// the active one is. Most cases below therefore run in the language the process
/// started in and still assert both forms -- proving the decision cannot depend
/// on the language setting.
/// </summary>
public sealed class LocalizedStatusDecisionTests
{
    [Theory]
    // The Chinese source form.
    [InlineData("已注册", true)]
    [InlineData("支付完成✅", true)]
    // ...and the English translation of the same row text.
    [InlineData("Registered", true)]
    [InlineData("Payment completed", true)]
    // A label family matches through its translations too: 已导入CPA renders as
    // "Imported CPA", and the decision must not need a literal for each member.
    [InlineData("Imported CPA", true)]
    [InlineData("已导入CPA", true)]
    // Brand text is language-independent already.
    [InlineData("PayPal 待支付", true)]
    // Negative: a pending payment is not a registered account.
    [InlineData("Pending payment", false)]
    [InlineData("待支付", false)]
    [InlineData("", false)]
    [InlineData("邮箱可用", false)]
    public void A_registered_row_is_recognized_whichever_language_rendered_it(string status, bool expected)
    {
        Assert.Equal(expected, RegisterViewModel.HasRegisteredAccountState(new PoolRow { Status = status }));
    }

    [Fact]
    public void An_english_labelled_registered_row_is_not_offered_for_registration_again()
    {
        // The regression that motivated the fix: the row carries a mailbox line,
        // so once the "registered" check missed, LocalUnregisteredVerdict
        // reported it as pending and the batch included it.
        var row = new PoolRow { Status = "Registered", MailboxLine = "a@example.com----pw----id----rt" };

        Assert.True(RegisterViewModel.HasRegisteredAccountState(row));
        Assert.False(RegisterViewModel.LocalUnregisteredVerdict(row, isCfWorker: true));
    }

    [Fact]
    public void The_registered_counter_counts_english_labelled_mailbox_rows()
    {
        var pool = new PoolViewModel();
        pool.AllRows.Add(new PoolRow { Identifier = "en@example.com", Status = "Registered", SourcePath = "pool.txt" });
        pool.AllRows.Add(new PoolRow { Identifier = "zh@example.com", Status = "已注册", SourcePath = "pool.txt" });
        pool.AllRows.Add(new PoolRow { Identifier = "pending@example.com", Status = "Pending payment", SourcePath = "pool.txt" });

        pool.UpdateOverview();

        Assert.Equal("2", pool.RegisteredCountText);
    }

    [Theory]
    [InlineData("Pending", true)]
    [InlineData("Pending payment", true)]
    [InlineData("Awaiting OTP", true)]
    [InlineData("Missing", true)]
    [InlineData("Failed", true)]
    // The probe-failure vocabulary is translated with "failure", not "Failed"
    // (网络失败 → "Network failure", 邮箱失败 → "Mailbox failure"). Matching only
    // keys that *begin* with 失败 kept the Chinese row in the attention filter and
    // silently dropped the English one -- the same asymmetry, mirrored.
    [InlineData("Network failure", true)]
    [InlineData("Mailbox failure", true)]
    [InlineData("待支付", true)]
    [InlineData("缺失", true)]
    [InlineData("失败", true)]
    [InlineData("网络失败", true)]
    [InlineData("邮箱失败", true)]
    [InlineData("Registered", false)]
    [InlineData("已注册", false)]
    // A non-failure label whose translation must not be swept in by the marker.
    [InlineData("邮箱可用", false)]
    public void Attention_filter_matches_both_languages(string status, bool expected)
    {
        Assert.Equal(expected, RegistrationStatusPresentation.NeedsAttention(new PoolRow { Status = status }));
    }

    [Theory]
    [InlineData("Registered", true)]
    [InlineData("Payment completed", true)]
    [InlineData("Imported CPA", true)]
    [InlineData("Pending payment", true)]
    [InlineData("已注册", true)]
    [InlineData("Failed", false)]
    public void Exportable_rows_are_recognized_in_both_languages(string status, bool expected)
    {
        var row = new PoolRow { Identifier = "a@example.com", Status = status };
        Assert.Equal(expected, ExportViewModel.IsImportable(row));
    }

    [Theory]
    [InlineData("network_failed")]
    [InlineData("mailbox_failed")]
    public void A_probe_failure_row_needs_attention_whichever_language_rendered_it(string machineStatus)
    {
        // The strongest form of the invariant: the *same* backend machine code must
        // produce the same verdict after a language switch. Asserting the literal
        // labels (as the theory above does) can stay green while a translation is
        // reworded; going through the interpreter makes the pair explicit.
        try
        {
            foreach (AppLanguage language in new[] { AppLanguage.En, AppLanguage.Zh })
            {
                Loc.Instance.SetLanguage(language);
                string text = AccountStatusInterpreter.DisplayAccountStatus(
                    machineStatus, "", "", "", "", "", "");
                var row = new PoolRow { Status = text };
                Assert.True(
                    RegistrationStatusPresentation.NeedsAttention(row),
                    $"{machineStatus} rendered as '{text}' in {language} must need attention");
            }
        }
        finally
        {
            Loc.Instance.SetLanguage(AppLanguage.Zh);
        }
    }

    [Theory]
    [InlineData("会话失效")]
    [InlineData("Session invalid")]
    public void A_session_invalid_row_is_a_pinned_attention_gap(string rendered)
    {
        // ``auth_state_failed`` renders as 会话失效 / "Session invalid". Neither form
        // contains an attention marker (待 / 缺 / 失败), so the row stays out of the
        // attention filter and counter while the badge paints it danger
        // (``StatusSeverityConverter`` matches 失效 / "Invalid").
        //
        // This is a *classification* gap shared by both languages, not the language
        // asymmetry fixed above, and closing it changes the attention count — a
        // product decision, not a refactor. Pinned so nobody mistakes it for
        // coverage: when the owner decides an invalid session should count, add
        // 失效/"Invalid" to the marker set and flip this assertion.
        Assert.False(RegistrationStatusPresentation.NeedsAttention(new PoolRow { Status = rendered }));
    }

    [Fact]
    public void The_predicate_does_not_depend_on_the_active_language()
    {
        // Explicit flip (the production scenario): a row rendered in Chinese must
        // still classify once the UI is English, and vice versa.
        try
        {
            Loc.Instance.SetLanguage(AppLanguage.En);
            Assert.True(RegisterViewModel.HasRegisteredAccountState(new PoolRow { Status = "已注册" }));
            Assert.True(RegistrationStatusPresentation.NeedsAttention(new PoolRow { Status = "待支付" }));

            Loc.Instance.SetLanguage(AppLanguage.Zh);
            Assert.True(RegisterViewModel.HasRegisteredAccountState(new PoolRow { Status = "Registered" }));
            Assert.True(RegistrationStatusPresentation.NeedsAttention(new PoolRow { Status = "Pending" }));
        }
        finally
        {
            Loc.Instance.SetLanguage(AppLanguage.Zh);
        }
    }
}
