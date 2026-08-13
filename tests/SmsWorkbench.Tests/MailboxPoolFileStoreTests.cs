using System.Text;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class MailboxPoolFileStoreTests
{
    [Theory]
    [InlineData(true, "200", "已获取")]
    [InlineData(true, "401", "401失效")]
    [InlineData(false, "401", "未获取")]
    public void AccessTokenDisplayReflectsProbeState(bool hasAccessToken, string statusCode, string expected)
    {
        Assert.Equal(expected, AccessTokenState.Display(hasAccessToken, statusCode));
    }

    [Theory]
    [InlineData("401", "at_invalid", "", "401")]
    [InlineData("", "at_invalid", "", "")]
    [InlineData("", "registered", "HTTP 401 unauthorized", "401")]
    [InlineData("200", "at_invalid", "", "200")]
    public void AccessTokenProbeCodeFallsBackToPersistedScanState(
        string explicitCode,
        string accountStatus,
        string error,
        string expected)
    {
        Assert.Equal(expected, AccessTokenState.ResolveProbeStatusCode(explicitCode, accountStatus, error));
    }

    [Fact]
    public void BuildReMailLineRequiresCompleteOrderIdentity()
    {
        Assert.Equal(
            "remail://user@example.com---service-token---order-123---purchase-456",
            MailboxPoolFileStore.BuildReMailLine("user@example.com", "service-token", "order-123", "purchase-456"));
        Assert.Empty(MailboxPoolFileStore.BuildReMailLine("user@example.com", "service-token", "", "purchase-456"));
    }

    [Theory]
    [InlineData("user@icloud.com----https://mail.example/inbox/private-token", "user@icloud.com")]
    [InlineData("user@me.com---http://mail.example/messages/private-token", "user@me.com")]
    public void ParsesICloudReceiveUrlLines(string line, string expectedEmail)
    {
        Assert.True(MailboxPoolFileStore.TryParseICloudUrlLine(line, out string email, out string receiveUrl));
        Assert.Equal(expectedEmail, email);
        Assert.StartsWith("http", receiveUrl, StringComparison.OrdinalIgnoreCase);
    }

    [Theory]
    [InlineData("user@example.com----https://mail.example/inbox/private-token")]
    [InlineData("user@icloud.com----ftp://mail.example/inbox/private-token")]
    [InlineData("user@icloud.com----not-a-url")]
    public void RejectsInvalidICloudReceiveUrlLines(string line)
    {
        Assert.False(MailboxPoolFileStore.TryParseICloudUrlLine(line, out _, out _));
    }

    [Theory]
    [InlineData("iCloud邮箱池", "icloud_url", true)]
    [InlineData("SQLite/iCloud", "icloud_url", true)]
    [InlineData("SQLite/Gmail", "gmail", true)]
    [InlineData("SQLite", "", false)]
    public void MailboxFilterIncludesRegisteredProviderAccounts(string accountType, string provider, bool expected)
    {
        Assert.Equal(expected, MailboxPoolFileStore.IsMailboxPoolLike(accountType, provider));
    }

    [Fact]
    public void ImportSupportedLinesAcceptsICloudAndDeduplicates()
    {
        string root = Path.Combine(Path.GetTempPath(), "smsworkbench-mailbox-import-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            string target = Path.Combine(root, "mailbox_tokens.txt");
            string icloud = "user@icloud.com----https://mail.example/inbox/private-token";
            string chatai = "other@example.com----password----client-id----refresh-token";

            (int imported, int skipped) = MailboxPoolFileStore.ImportSupportedLines(
                target,
                new[] { icloud, chatai, icloud, "invalid" });

            Assert.Equal(2, imported);
            Assert.Equal(2, skipped);
            Assert.Equal(new[] { icloud, chatai }, File.ReadAllLines(target, Encoding.UTF8));
            Assert.False(File.ReadAllBytes(target).Take(3).SequenceEqual(new byte[] { 0xEF, 0xBB, 0xBF }));
        }
        finally
        {
            Directory.Delete(root, true);
        }
    }

    [Fact]
    public void DeleteMatchingLinesRemovesDuplicatesWithoutAddingBom()
    {
        string root = Path.Combine(Path.GetTempPath(), "smsworkbench-mailbox-delete-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            string selected = Path.Combine(root, "imported-pool.txt");
            File.WriteAllText(selected, "", Encoding.UTF8);

            string target = "User+alias@outlook.com";
            string other = "other@example.com";
            string targetLine = target + "----password----client----refresh";
            File.WriteAllLines(selected, new[]
            {
                "# retained comment",
                targetLine,
                "gmail://" + target.ToUpperInvariant() + "---app-password",
                "remail://" + target.ToUpperInvariant() + "---service-token---order-123---purchase-456",
                other + "---password---refresh",
                targetLine
            }, new UTF8Encoding(true));

            int removed = MailboxPoolFileStore.DeleteMatchingLines(
                selected,
                MailboxPoolFileStore.NormalizeEmailKey(target),
                new[] { targetLine });

            Assert.Equal(4, removed);
            Assert.Equal(new[] { "# retained comment", other + "---password---refresh" }, File.ReadAllLines(selected, Encoding.UTF8));
            Assert.False(File.ReadAllBytes(selected).Take(3).SequenceEqual(new byte[] { 0xEF, 0xBB, 0xBF }));
        }
        finally
        {
            Directory.Delete(root, true);
        }
    }
}
