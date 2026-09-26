using SmsWorkbench;
using Wpf.Ui.Appearance;

namespace SmsWorkbench.Tests;

public sealed class DesktopThemePreferenceTests
{
    [Theory]
    [InlineData(ApplicationTheme.Light)]
    [InlineData(ApplicationTheme.Dark)]
    public void MissingOrInvalidPreferenceUsesSystemTheme(ApplicationTheme systemTheme)
    {
        using var fixture = new TemporaryDirectory();
        Assert.Equal(systemTheme, DesktopThemePreference.Load(fixture.Path, systemTheme));
        Assert.False(Directory.Exists(Path.Combine(fixture.Path, "runtime")));

        string path = Path.Combine(fixture.Path, "runtime", "desktop_theme.txt");
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, "unknown");
        Assert.Equal(systemTheme, DesktopThemePreference.Load(fixture.Path, systemTheme));
    }

    [Fact]
    public void TogglePreferenceSurvivesRestartAndOverridesSystemTheme()
    {
        using var fixture = new TemporaryDirectory();
        DesktopThemePreference.Save(fixture.Path, ApplicationTheme.Dark);
        Assert.Equal(ApplicationTheme.Dark, DesktopThemePreference.Load(fixture.Path, ApplicationTheme.Light));

        DesktopThemePreference.Save(fixture.Path, ApplicationTheme.Light);
        Assert.Equal(ApplicationTheme.Light, DesktopThemePreference.Load(fixture.Path, ApplicationTheme.Dark));
    }

    [Fact]
    public void ThemePreferenceDoesNotChangeSharedRuntimeConfig()
    {
        using var fixture = new TemporaryDirectory();
        string config = Path.Combine(fixture.Path, "runtime.json");
        File.WriteAllText(config, "{\"runtime\":{\"python_path\":\"python\"}}");

        DesktopThemePreference.Save(fixture.Path, ApplicationTheme.Dark);

        Assert.Equal("{\"runtime\":{\"python_path\":\"python\"}}", File.ReadAllText(config));
        Assert.Equal(ApplicationTheme.Dark, DesktopThemePreference.Load(fixture.Path, ApplicationTheme.Light));
    }
}
