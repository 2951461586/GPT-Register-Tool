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

    [Fact]
    public void ModeRoundTripsThroughSystemLightAndDark()
    {
        using var fixture = new TemporaryDirectory();

        DesktopThemePreference.SaveMode(fixture.Path, DesktopThemeMode.Dark);
        Assert.Equal(DesktopThemeMode.Dark, DesktopThemePreference.LoadMode(fixture.Path));
        Assert.Equal(ApplicationTheme.Dark, DesktopThemePreference.Load(fixture.Path, ApplicationTheme.Light));

        DesktopThemePreference.SaveMode(fixture.Path, DesktopThemeMode.Light);
        Assert.Equal(DesktopThemeMode.Light, DesktopThemePreference.LoadMode(fixture.Path));

        // "Follow system" stores the mode; the palette is resolved on read, so the same
        // file answers Light on a light machine and Dark on a dark one.
        DesktopThemePreference.SaveMode(fixture.Path, DesktopThemeMode.System);
        Assert.Equal(DesktopThemeMode.System, DesktopThemePreference.LoadMode(fixture.Path));
        Assert.Equal(ApplicationTheme.Light, DesktopThemePreference.Load(fixture.Path, ApplicationTheme.Light));
        Assert.Equal(ApplicationTheme.Dark, DesktopThemePreference.Load(fixture.Path, ApplicationTheme.Dark));
    }

    [Fact]
    public void ModeCycleIsLightThenDarkThenSystem()
    {
        // The sidebar button's order. Two clicks from either palette land on "follow
        // system", and one click from the default mode is the light palette.
        Assert.Equal(DesktopThemeMode.Dark, DesktopThemeMode.Light.Next());
        Assert.Equal(DesktopThemeMode.System, DesktopThemeMode.Dark.Next());
        Assert.Equal(DesktopThemeMode.Light, DesktopThemeMode.System.Next());
    }

    [Theory]
    [InlineData("Light", DesktopThemeMode.Light)]
    [InlineData("Dark", DesktopThemeMode.Dark)]
    [InlineData("System", DesktopThemeMode.System)]
    [InlineData("dark", DesktopThemeMode.Dark)]
    [InlineData("nonsense", DesktopThemeMode.System)]
    [InlineData("", DesktopThemeMode.System)]
    public void PreferenceTextParsesEveryStorableMode(string text, DesktopThemeMode expected)
    {
        Assert.True(DesktopThemeModeExtensions.TryParsePreference(text, out DesktopThemeMode mode) || expected == DesktopThemeMode.System);
        Assert.Equal(expected, mode);
    }
}
