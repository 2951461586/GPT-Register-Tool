using System.Text;
using SmsWorkbench;

Assert(AccessTokenState.Display(true, "200") == "已获取", "HTTP 200 AT should display as acquired");
Assert(AccessTokenState.Display(true, "401") == "401失效", "HTTP 401 AT should display as invalid");
Assert(AccessTokenState.Display(false, "401") == "未获取", "missing AT must not display as a retained invalid token");

string remailLine = MailboxPoolFileStore.BuildReMailLine(
    "user@example.com",
    "service-token",
    "order-123",
    "purchase-456");
Assert(
    remailLine == "remail://user@example.com---service-token---order-123---purchase-456",
    "ReMail credentials should be serialized in the mailbox parser format");
Assert(
    MailboxPoolFileStore.BuildReMailLine("user@example.com", "service-token", "", "purchase-456") == "",
    "ReMail credentials without an order number must not be treated as complete");

string root = Path.Combine(Path.GetTempPath(), "smsworkbench-mailbox-delete-" + Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(root);
try
{
    string selected = Path.Combine(root, "imported-pool.txt");
    string token = Path.Combine(root, "tokens.txt");
    string hotmail = Path.Combine(root, "hotmail.txt");
    string chatai = Path.Combine(root, "chatai_extra.txt");
    File.WriteAllText(selected, "", Encoding.UTF8);
    File.WriteAllText(token, "", Encoding.UTF8);
    File.WriteAllText(hotmail, "", Encoding.UTF8);
    File.WriteAllText(chatai, "", Encoding.UTF8);

    IReadOnlyList<string> known = MailboxPoolFileStore.DiscoverKnownFiles(root, token, selected);
    Assert(known.Contains(selected, StringComparer.OrdinalIgnoreCase), "selected mailbox file was not discovered");
    Assert(known.Contains(token, StringComparer.OrdinalIgnoreCase), "configured token file was not discovered");
    Assert(known.Contains(hotmail, StringComparer.OrdinalIgnoreCase), "hotmail.txt was not discovered");
    Assert(known.Contains(chatai, StringComparer.OrdinalIgnoreCase), "chatai wildcard file was not discovered");

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

    Assert(removed == 4, "all duplicate target mailbox lines must be removed");
    string[] remaining = File.ReadAllLines(selected, Encoding.UTF8);
    Assert(remaining.SequenceEqual(new[] { "# retained comment", other + "---password---refresh" }), "unrelated lines changed");
    Assert(File.ReadAllBytes(selected).Take(3).SequenceEqual(new byte[] { 0x23, 0x20, 0x72 }), "rewritten pool should not contain a UTF-8 BOM");

    Console.WriteLine("Mailbox pool deletion regression tests passed.");
}
finally
{
    Directory.Delete(root, true);
}

static void Assert(bool condition, string message)
{
    if (!condition) throw new InvalidOperationException(message);
}
