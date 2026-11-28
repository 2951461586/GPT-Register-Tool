#nullable enable

using System.Text.RegularExpressions;

namespace SmsWorkbench.Tests;

public sealed class ThemePresetEngineTests
{
    private static readonly Regex XamlKeyRegex = new(@"x:Key=""([^""]+)""", RegexOptions.Compiled);

    [Fact]
    public void EveryBuiltInPresetMatchesFullKeySetAndPassesWcagAa()
    {
        HashSet<string> expectedKeys = XamlKeyRegex
            .Matches(RepoFile.Read("SmsWorkbench/Themes/Colors.Light.xaml"))
            .Select(m => m.Groups[1].Value)
            .ToHashSet(StringComparer.Ordinal);

        Assert.True(ThemePresetEngine.BuiltInPresets.Count >= 5);

        foreach (ThemePresetDefinition preset in ThemePresetEngine.BuiltInPresets)
        {
            foreach (bool isDark in new[] { false, true })
            {
                ThemeSchemeSeed seed = isDark ? preset.Dark : preset.Light;
                Dictionary<string, RgbColor> map = ThemePresetEngine.BuildColorMap(seed, isDark);

                Assert.Equal(expectedKeys.OrderBy(k => k), map.Keys.OrderBy(k => k));

                ThemeContrastReport report = ThemePresetEngine.EvaluateContrast(map);
                Assert.True(
                    report.PassesAa,
                    $"Preset '{preset.Id}' (dark={isDark}) failed WCAG AA: {string.Join("; ", report.Warnings)}");

                // Surface ladder direction invariant
                double page = ThemePresetEngine.RelativeLuminance(map["Surface.Page"]);
                double card = ThemePresetEngine.RelativeLuminance(map["Surface.Card"]);
                double block = ThemePresetEngine.RelativeLuminance(map["Surface.Block"]);
                double inset = ThemePresetEngine.RelativeLuminance(map["Surface.Inset"]);
                if (isDark)
                {
                    Assert.True(card > page && block > card && inset > block, $"Dark ladder broken for {preset.Id}");
                }
                else
                {
                    Assert.True(card > page && block < card && inset < block, $"Light ladder broken for {preset.Id}");
                }
            }
        }
    }

    [Fact]
    public void TweakCnCssParserSupportsOklchHslHexAndRadius()
    {
        const string css = """
            :root {
              --background: oklch(0.98 0.01 95);
              --foreground: oklch(0.20 0.02 260);
              --card: #ffffff;
              --muted: 240 4.8% 95.9%;
              --muted-foreground: hsl(240 3.8% 46.1%);
              --primary: oklch(0.55 0.18 155);
              --primary-foreground: #ffffff;
              --destructive: rgb(220, 38, 38);
              --border: #e4e4e7;
              --radius: 0.75rem;
            }
            .dark {
              --background: oklch(0.14 0.01 260);
              --foreground: oklch(0.96 0.01 260);
              --card: oklch(0.19 0.01 260);
              --primary: oklch(0.72 0.16 155);
              --primary-foreground: #091c14;
              --border: #27272a;
            }
            """;

        TweakCnParseResult parsed = ThemePresetEngine.TryParseTweakCnCss(css);
        Assert.True(parsed.Ok, parsed.ErrorMessage);
        Assert.NotNull(parsed.Preset);
        Assert.Equal(CornerDensity.Soft, parsed.SuggestedCornerDensity);

        Dictionary<string, RgbColor> lightMap = ThemePresetEngine.BuildColorMap(parsed.Preset!.Light, isDark: false);
        Dictionary<string, RgbColor> darkMap = ThemePresetEngine.BuildColorMap(parsed.Preset!.Dark, isDark: true);
        ThemeContrastReport lightReport = ThemePresetEngine.EvaluateContrast(lightMap);
        ThemeContrastReport darkReport = ThemePresetEngine.EvaluateContrast(darkMap);
        Assert.True(lightReport.PassesAa, string.Join("; ", lightReport.Warnings));
        Assert.True(darkReport.PassesAa, string.Join("; ", darkReport.Warnings));
    }

    [Fact]
    public void TweakCnCssParserRejectsBlankOrVariableFreeInput()
    {
        Assert.False(ThemePresetEngine.TryParseTweakCnCss("").Ok);
        Assert.False(ThemePresetEngine.TryParseTweakCnCss("body { color: red; }").Ok);
    }

    [Fact]
    public void AppearancePreferencesRoundTripAtomically()
    {
        using var fixture = new TemporaryDirectory();
        var original = new DesktopAppearancePreferences(
            PresetId: "claude",
            CornerDensity: CornerDensity.Sharp,
            CustomTweakCnCss: ":root { --primary: #2563eb; }");

        DesktopAppearancePreference.Save(fixture.Path, original);
        DesktopAppearancePreferences loaded = DesktopAppearancePreference.Load(fixture.Path);

        Assert.Equal(original, loaded);
    }

    [Fact]
    public async Task ProtocolCardPaymentStepperTracksQuotePaidAndChallengeStates()
    {
        var stub = new ScriptedCardPayService();
        var vm = new ProtocolCardPaymentViewModel(
            stub,
            initialCheckoutUrl: "https://checkout.stripe.com/c/pay/cs_step_test")
        {
            PublishableKey = "pk_test_12345",
            ReadOnlyMode = true,
        };

        Assert.Equal(0, vm.ActiveStepIndex);
        Assert.Equal("idle", vm.StepState);

        stub.NextResult = MakeResult(ok: true, quoteOnly: true, state: "quoted");
        await vm.QuoteCommand.ExecuteAsync(null);
        Assert.Equal(1, vm.ActiveStepIndex);
        Assert.Equal("success", vm.StepState);

        stub.NextResult = MakeResult(ok: true, quoteOnly: false, state: "read_only");
        await vm.PayCommand.ExecuteAsync(null);
        Assert.Equal(3, vm.ActiveStepIndex);
        Assert.Equal("success", vm.StepState);

        stub.NextResult = MakeResult(ok: false, quoteOnly: false, state: "challenge_required", manualAction: true);
        await vm.PayCommand.ExecuteAsync(null);
        Assert.Equal(2, vm.ActiveStepIndex);
        Assert.Equal("warn", vm.StepState);
    }

    private static ProtocolCardPaymentResult MakeResult(bool ok, bool quoteOnly, string state, bool manualAction = false)
        => new(ok, quoteOnly, state, "hosted", "cs_step_test", "https://checkout.stripe.com/c/pay/cs_step_test", 0, "USD", "$0.00", "", "", "", "", manualAction, "", "", "", "", "ok", "", "{}");

    private sealed class ScriptedCardPayService : IProtocolCardPaymentService
    {
        public ProtocolCardPaymentResult NextResult { get; set; } = MakeResult(true, true, "quoted");
        public string ResolveDefaultProxy() => "";
        public Task<ProtocolCardPaymentResult> ExecuteAsync(ProtocolCardPaymentRequest request, CancellationToken ct)
            => Task.FromResult(NextResult);
    }
}
