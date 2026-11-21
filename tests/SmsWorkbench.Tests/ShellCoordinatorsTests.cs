#nullable enable

using System.Text.Json;

namespace SmsWorkbench.Tests;

public sealed class ShellCoordinatorsTests
{
    [Fact]
    public void ShellViewModel_NavKeyLifecycleAndSidebarToggle()
    {
        var shell = new ShellViewModel();
        bool? observedCollapse = null;
        shell.SidebarCollapsedChanged += (_, collapsed) => observedCollapse = collapsed;

        shell.SidebarCollapsed = true;
        Assert.True(shell.SidebarCollapsed);
        Assert.Equal(true, observedCollapse);

        shell.ParkPendingNavKey("register");
        Assert.Equal(string.Empty, shell.ActiveNavKey);
        Assert.Equal("register", shell.TakePendingNavKey());
        Assert.Equal(string.Empty, shell.TakePendingNavKey());

        shell.ActiveNavKey = "register";
        shell.ClearActiveNavKey("other");
        Assert.Equal("register", shell.ActiveNavKey);
        shell.ClearActiveNavKey("register");
        Assert.Equal(string.Empty, shell.ActiveNavKey);
    }

    [Fact]
    public void PoolRefreshCoordinator_DeduplicatesByPriorityAndRestoresChecked()
    {
        var rows = new List<PoolRow>
        {
            new() { Identifier = "A@Example.com", SourcePath = "pool.txt", AccountType = "邮箱池", IsChecked = true },
            new() { Identifier = "a@example.com", SourcePath = "accounts.sqlite3", AccountType = "Session" },
            new() { Identifier = "b@example.com", SourcePath = "pool.txt", AccountType = "邮箱池", IsChecked = false },
        };

        HashSet<string> checkedIds = PoolRefreshCoordinator.CaptureCheckedIdentifiers(rows);
        List<PoolRow> deduped = PoolRefreshCoordinator.DeduplicateRows(rows);
        PoolRefreshCoordinator.RestoreCheckedIdentifiers(deduped, checkedIds);

        Assert.Equal(2, deduped.Count);
        PoolRow aRow = Assert.Single(deduped, r => r.Identifier.Equals("a@example.com", StringComparison.OrdinalIgnoreCase));
        Assert.Equal("accounts.sqlite3", aRow.SourcePath);
        Assert.True(aRow.IsChecked);
    }

    [Fact]
    public void ThemeCoordinator_CyclesModesAndValidatesSchemeSources()
    {
        var coordinator = new ThemeCoordinator();
        Assert.Equal(DesktopThemeMode.System, coordinator.CurrentMode);
        Assert.Equal(DesktopThemeMode.Light, coordinator.NextMode());

        coordinator.SetMode(DesktopThemeMode.Dark);
        Assert.Equal(DesktopThemeMode.Dark, coordinator.CurrentMode);
        Assert.Equal("Colors.Dark.xaml", ThemeCoordinator.WantedSchemeFileName(isDark: true));
        Assert.True(ThemeCoordinator.IsColorSchemeSource("Themes/Colors.Light.xaml"));
        Assert.False(ThemeCoordinator.IsColorSchemeSource("Themes/Tokens.xaml"));
    }

    [Fact]
    public void BackendBatchCoordinator_ParsesDoctorAndThrottlesHotRefresh()
    {
        using JsonDocument doc = JsonDocument.Parse(
            """
            {
              "checks": [
                {"name": "python", "status": "ok"},
                {"name": "curl_cffi", "status": "fail", "hint": "install curl_cffi"},
                {"name": "proxy", "status": "warn"}
              ]
            }
            """);

        (List<string> fails, int warned) = BackendBatchCoordinator.ParseDoctorPayload(doc.RootElement);
        Assert.Single(fails);
        Assert.Contains("curl_cffi: install curl_cffi", fails[0], StringComparison.Ordinal);
        Assert.Equal(1, warned);

        var coordinator = new BackendBatchCoordinator();
        DateTime t0 = new(2026, 11, 21, 8, 0, 0, DateTimeKind.Utc);
        Assert.True(coordinator.TryClaimHotRefreshSlot(t0));
        Assert.False(coordinator.TryClaimHotRefreshSlot(t0.AddMilliseconds(200)));
        Assert.True(coordinator.TryClaimHotRefreshSlot(t0.AddMilliseconds(800)));
    }

    [Fact]
    public void RegistrationWorkflowCoordinator_BuildsPlansAndRejectsUnknownSource()
    {
        var options = new RegisterOptions
        {
            Source = "remail_target",
            Count = 5,
            Workers = 2,
            Disable2fa = true,
            CheckPromotion = true,
        };
        BackendCommandPlan? plan = RegistrationWorkflowCoordinator.TryBuildSourcePlan(
            options,
            new[] { "http://127.0.0.1:7890" },
            () => "cf.example",
            () => "smailr.com",
            () => "",
            _ => { },
            out string? errorTitle,
            out string? errorMessage);

        Assert.NotNull(plan);
        Assert.Null(errorTitle);
        Assert.Null(errorMessage);
        Assert.Contains("--target-at200", plan!.Arguments);

        var unknown = new RegisterOptions { Source = "nonexistent", Count = 1, Workers = 1 };
        BackendCommandPlan? failed = RegistrationWorkflowCoordinator.TryBuildSourcePlan(
            unknown,
            Array.Empty<string>(),
            () => "",
            () => "",
            () => "",
            _ => { },
            out errorTitle,
            out errorMessage);
        Assert.Null(failed);
        Assert.NotNull(errorTitle);
        Assert.NotNull(errorMessage);
    }
}
