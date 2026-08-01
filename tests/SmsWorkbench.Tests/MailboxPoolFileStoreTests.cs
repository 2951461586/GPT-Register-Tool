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

    [Fact]
    public void BuildReMailLineRequiresCompleteOrderIdentity()
    {
        Assert.Equal(
            "remail://user@example.com---service-token---order-123---purchase-456",
            MailboxPoolFileStore.BuildReMailLine("user@example.com", "service-token", "order-123", "purchase-456"));
        Assert.Empty(MailboxPoolFileStore.BuildReMailLine("user@example.com", "service-token", "", "purchase-456"));
    }

    [Fact]
    public void DeleteMatchingLinesRemovesDuplicatesWithoutAddingBom()
    {
        string root = Path.Combine(Path.GetTempPath(), "smsworkbench-mailbox-delete-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            string selected = Path.Combine(root, "imported-pool.txt");
            string token = Path.Combine(root, "tokens.txt");
            string hotmail = Path.Combine(root, "hotmail.txt");
            string chatai = Path.Combine(root, "chatai_extra.txt");
            foreach (string path in new[] { selected, token, hotmail, chatai })
                File.WriteAllText(path, "", Encoding.UTF8);

            IReadOnlyList<string> known = MailboxPoolFileStore.DiscoverKnownFiles(root, token, selected);
            Assert.Contains(selected, known, StringComparer.OrdinalIgnoreCase);
            Assert.Contains(token, known, StringComparer.OrdinalIgnoreCase);
            Assert.Contains(hotmail, known, StringComparer.OrdinalIgnoreCase);
            Assert.Contains(chatai, known, StringComparer.OrdinalIgnoreCase);

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
