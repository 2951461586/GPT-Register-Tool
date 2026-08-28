using System.Text.Json;

namespace SmsWorkbench.WebHost.Tests;

public sealed class AccountCatalogTests
{
    [Fact]
    public void MapWhitelistsFieldsAndExcludesSecrets()
    {
        string json = """
            {
              "id": "acc-1",
              "email": "user@example.test",
              "success": true,
              "status": "active",
              "register_method": "protocol",
              "session_type": "oauth",
              "plan_type": "free",
              "account_type": "chatgpt",
              "promotion_status": "Free·无优惠",
              "refresh_token_status": "oauth_present",
              "at_probe_status_code": "200",
              "access_token_present": true,
              "refresh_token_present": true,
              "totp_present": false,
              "workspace_status": "active",
              "workspace_name": "default",
              "registration_state": "active",
              "registration_country": "US",
              "mailbox_provider": "remail",
              "mailbox_source": "pool",
              "batch_id": "batch-001",
              "created_at": "2026-01-01T00:00:00Z",
              "updated_at": "2026-01-02T00:00:00Z",
              "session": { "access_token": "sk-xxx" },
              "json_path": "/secret/path.json",
              "device_id": "dev-xxx"
            }
            """;
        JsonElement element = JsonDocument.Parse(json).RootElement;
        AccountSummaryDto dto = PythonAccountCatalog.Map(element);

        Assert.Equal("acc-1", dto.Id);
        Assert.Equal("user@example.test", dto.Email);
        Assert.True(dto.Success);
        Assert.Equal("active", dto.Status);
        Assert.Equal("free", dto.PlanType);
        Assert.True(dto.AccessTokenPresent);
        Assert.True(dto.RefreshTokenPresent);
        Assert.False(dto.TotpPresent);
    }

    [Fact]
    public void MapHandlesMissingFieldsGracefully()
    {
        string json = """{"id": "x", "email": "a@b.test"}""";
        JsonElement element = JsonDocument.Parse(json).RootElement;
        AccountSummaryDto dto = PythonAccountCatalog.Map(element);

        Assert.Equal("x", dto.Id);
        Assert.Equal("a@b.test", dto.Email);
        Assert.False(dto.Success);
        Assert.Equal("", dto.Status);
        Assert.Equal("", dto.PlanType);
        Assert.False(dto.AccessTokenPresent);
    }
}
