using System.Text.Json;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class DesktopReadClientTests
{
    [Fact]
    public async Task ReadAccountsUsesDesktopReadContract()
    {
        var backend = new StubBackendClient
        {
            Handler = _ => new BackendCommandResult(
                0, "", "", JsonElementOf("{\"ok\":true,\"accounts\":[{\"email\":\"a@example.test\"}]}"), false)
        };
        var client = new DesktopReadClient(new BackendTaskCoordinator(backend));

        JsonElement payload = await client.ReadAccountsAsync();

        Assert.True(payload.GetProperty("ok").GetBoolean());
        Assert.Equal("a@example.test", payload.GetProperty("accounts")[0].GetProperty("email").GetString());
        Assert.NotNull(backend.LastCommand);
        Assert.Equal(
            new[] { "--desktop-read", "accounts", "--desktop-ipc" },
            backend.LastCommand.Arguments.ToArray());
    }

    [Fact]
    public async Task ReadMailboxPoolUsesDesktopReadContract()
    {
        var backend = new StubBackendClient
        {
            Handler = _ => new BackendCommandResult(
                0, "", "",
                JsonElementOf("{\"ok\":true,\"files\":[{\"path\":\"mailbox_tokens.txt\",\"name\":\"mailbox_tokens.txt\",\"lines\":[{\"raw_line\":\"remail://a@example.test---token---order\",\"email\":\"a@example.test\",\"provider\":\"remail\",\"token\":\"token\"}]}]}"),
                false)
        };
        var client = new DesktopReadClient(new BackendTaskCoordinator(backend));

        JsonElement payload = await client.ReadMailboxPoolAsync();

        Assert.True(payload.GetProperty("ok").GetBoolean());
        JsonElement line = payload.GetProperty("files")[0].GetProperty("lines")[0];
        Assert.Equal("a@example.test", line.GetProperty("email").GetString());
        Assert.Equal("remail", line.GetProperty("provider").GetString());
        Assert.NotNull(backend.LastCommand);
        Assert.Equal(
            new[] { "--desktop-read", "mailbox-pool", "--desktop-ipc" },
            backend.LastCommand.Arguments.ToArray());
    }

    [Fact]
    public async Task ReadMailboxPoolPassesSelectedFile()
    {
        var backend = new StubBackendClient
        {
            Handler = _ => new BackendCommandResult(
                0, "", "", JsonElementOf("{\"ok\":true,\"files\":[]}"), false)
        };
        var client = new DesktopReadClient(new BackendTaskCoordinator(backend));

        await client.ReadMailboxPoolAsync("C:\\pool\\selected.txt");

        Assert.NotNull(backend.LastCommand);
        Assert.Equal(
            new[] { "--desktop-read", "mailbox-pool", "--desktop-ipc", "--chatai-mailbox-file", "C:\\pool\\selected.txt" },
            backend.LastCommand.Arguments.ToArray());
    }

    [Fact]
    public async Task ReadAccountBuildsScopedArguments()
    {
        var backend = new StubBackendClient
        {
            Handler = _ => new BackendCommandResult(
                0, "", "", JsonElementOf("{\"ok\":true,\"account\":{\"email\":\"a@example.test\"}}"), false)
        };
        var client = new DesktopReadClient(new BackendTaskCoordinator(backend));

        JsonElement payload = await client.ReadAccountAsync("42", "a@example.test");

        Assert.True(payload.GetProperty("ok").GetBoolean());
        Assert.NotNull(backend.LastCommand);
        Assert.Equal(
            new[] { "--desktop-read", "account", "--desktop-ipc", "--account-id", "42", "--email", "a@example.test" },
            backend.LastCommand.Arguments.ToArray());
    }

    [Fact]
    public async Task ReadAccountOmitsEmptySelectors()
    {
        var backend = new StubBackendClient
        {
            Handler = _ => new BackendCommandResult(
                0, "", "", JsonElementOf("{\"ok\":true,\"account\":{}}"), false)
        };
        var client = new DesktopReadClient(new BackendTaskCoordinator(backend));

        await client.ReadAccountAsync("", "");

        Assert.NotNull(backend.LastCommand);
        Assert.Equal(
            new[] { "--desktop-read", "account", "--desktop-ipc" },
            backend.LastCommand.Arguments.ToArray());
    }

    [Fact]
    public async Task ReadMailboxLineReadsAndDeletesBackendTempFile()
    {
        string tempPath = TempFilePath("smsworkbench_mailbox_");
        var backend = new StubBackendClient
        {
            Handler = _ =>
            {
                File.WriteAllText(tempPath, "remail://a@example.test---token---order\n");
                return new BackendCommandResult(
                    0, "", "", JsonElementOf("{\"ok\":true,\"path\":" + JsonSerializer.Serialize(tempPath) + "}"), false);
            }
        };
        var client = new DesktopReadClient(new BackendTaskCoordinator(backend));

        string line = await client.ReadMailboxLineAsync("7", "a@example.test");

        Assert.Equal("remail://a@example.test---token---order", line.Trim());
        Assert.False(File.Exists(tempPath));
        Assert.NotNull(backend.LastCommand);
        Assert.Equal(
            new[] { "--desktop-read", "mailbox-file", "--desktop-ipc", "--account-id", "7", "--email", "a@example.test" },
            backend.LastCommand.Arguments.ToArray());
    }

    [Fact]
    public async Task ReadPaymentUrlReadsAndDeletesBackendTempFile()
    {
        string tempPath = TempFilePath("smsworkbench_payment_url_");
        var backend = new StubBackendClient
        {
            Handler = _ =>
            {
                File.WriteAllText(tempPath, "https://pay.openai.com/c/pay/cs_test\n");
                return new BackendCommandResult(
                    0, "", "", JsonElementOf("{\"ok\":true,\"path\":" + JsonSerializer.Serialize(tempPath) + "}"), false);
            }
        };
        var client = new DesktopReadClient(new BackendTaskCoordinator(backend));

        string url = await client.ReadPaymentUrlAsync("", "a@example.test");

        Assert.Equal("https://pay.openai.com/c/pay/cs_test", url.Trim());
        Assert.False(File.Exists(tempPath));
        Assert.NotNull(backend.LastCommand);
        Assert.Equal(
            new[] { "--desktop-read", "payment-url-file", "--desktop-ipc", "--email", "a@example.test" },
            backend.LastCommand.Arguments.ToArray());
    }

    [Fact]
    public async Task ReadAccountExportParsesBackendTempJson()
    {
        string tempPath = TempFilePath("smsworkbench_account_");
        var backend = new StubBackendClient
        {
            Handler = _ =>
            {
                File.WriteAllText(tempPath, "{\"email\":\"a@example.test\",\"access_token\":\"secret\"}");
                return new BackendCommandResult(
                    0, "", "", JsonElementOf("{\"ok\":true,\"path\":" + JsonSerializer.Serialize(tempPath) + "}"), false);
            }
        };
        var client = new DesktopReadClient(new BackendTaskCoordinator(backend));

        JsonElement account = await client.ReadAccountExportAsync("9", "a@example.test");

        Assert.Equal("a@example.test", account.GetProperty("email").GetString());
        Assert.Equal("secret", account.GetProperty("access_token").GetString());
        Assert.False(File.Exists(tempPath));
        Assert.NotNull(backend.LastCommand);
        Assert.Equal(
            new[] { "--desktop-read", "account-file", "--desktop-ipc", "--account-id", "9", "--email", "a@example.test" },
            backend.LastCommand.Arguments.ToArray());
    }

    [Fact]
    public async Task ReadRejectsTempPathOutsideExpectedPrefix()
    {
        string tempPath = TempFilePath("unexpected_prefix_");
        var backend = new StubBackendClient
        {
            Handler = _ => new BackendCommandResult(
                0, "", "", JsonElementOf("{\"ok\":true,\"path\":" + JsonSerializer.Serialize(tempPath) + "}"), false)
        };
        var client = new DesktopReadClient(new BackendTaskCoordinator(backend));

        InvalidOperationException error = await Assert.ThrowsAsync<InvalidOperationException>(
            () => client.ReadMailboxLineAsync("7", "a@example.test"));

        Assert.Contains("invalid temporary file path", error.Message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task ReadThrowsBackendErrorPayload()
    {
        var backend = new StubBackendClient
        {
            Handler = _ => new BackendCommandResult(
                3, "", "", JsonElementOf("{\"ok\":false,\"error\":\"account_not_found\"}"), false)
        };
        var client = new DesktopReadClient(new BackendTaskCoordinator(backend));

        InvalidOperationException error = await Assert.ThrowsAsync<InvalidOperationException>(
            () => client.ReadAccountsAsync());

        Assert.Contains("account_not_found", error.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task ReadThrowsWhenPayloadMissing()
    {
        var backend = new StubBackendClient
        {
            Handler = _ => new BackendCommandResult(1, "", "boom", null, false)
        };
        var client = new DesktopReadClient(new BackendTaskCoordinator(backend));

        InvalidOperationException error = await Assert.ThrowsAsync<InvalidOperationException>(
            () => client.ReadAccountsAsync());

        Assert.Contains("no payload", error.Message, StringComparison.OrdinalIgnoreCase);
    }

    private static string TempFilePath(string prefix)
        => Path.Combine(Path.GetTempPath(), prefix + Guid.NewGuid().ToString("N") + ".tmp");

    private static JsonElement JsonElementOf(string json)
    {
        using JsonDocument document = JsonDocument.Parse(json);
        return document.RootElement.Clone();
    }
}
