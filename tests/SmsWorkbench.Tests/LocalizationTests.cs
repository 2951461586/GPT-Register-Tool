using System.Globalization;
using SmsWorkbench.Localization;

namespace SmsWorkbench.Tests;

public sealed class LocalizationTests
{
    [Fact]
    public void The_source_string_is_the_key_so_the_chinese_build_needs_no_catalog()
    {
        Loc.Instance.SetLanguage(AppLanguage.Zh);
        Assert.Equal("一键注册", Loc.T("一键注册"));
    }

    [Fact]
    public void Switching_to_english_translates_and_switching_back_restores_the_source()
    {
        try
        {
            Loc.Instance.SetLanguage(AppLanguage.En);
            Assert.Equal("Quick register", Loc.T("一键注册"));
            Assert.Equal("Batch 3", Loc.F("批次 {0}", 3));

            Loc.Instance.SetLanguage(AppLanguage.Zh);
            Assert.Equal("一键注册", Loc.T("一键注册"));
            Assert.Equal("批次 3", Loc.F("批次 {0}", 3));
        }
        finally
        {
            Loc.Instance.SetLanguage(AppLanguage.Zh);
        }
    }

    [Fact]
    public void An_unknown_key_falls_back_to_the_key_instead_of_throwing()
    {
        try
        {
            Loc.Instance.SetLanguage(AppLanguage.En);
            Assert.Equal("没有这条翻译", Loc.T("没有这条翻译"));
        }
        finally
        {
            Loc.Instance.SetLanguage(AppLanguage.Zh);
        }
    }

    [Fact]
    public void A_switch_invalidates_every_binding_and_raises_the_language_event()
    {
        bool languageChanged = false;
        var changedProperties = new List<string?>();
        void OnLanguageChanged(object? sender, EventArgs e) => languageChanged = true;
        void OnPropertyChanged(object? sender, System.ComponentModel.PropertyChangedEventArgs e)
            => changedProperties.Add(e.PropertyName);
        try
        {
            Loc.Instance.SetLanguage(AppLanguage.Zh);
            Loc.Instance.LanguageChanged += OnLanguageChanged;
            Loc.Instance.PropertyChanged += OnPropertyChanged;

            Loc.Instance.SetLanguage(AppLanguage.En);

            Assert.True(languageChanged);
            Assert.Contains(string.Empty, changedProperties);
        }
        finally
        {
            Loc.Instance.LanguageChanged -= OnLanguageChanged;
            Loc.Instance.PropertyChanged -= OnPropertyChanged;
            Loc.Instance.SetLanguage(AppLanguage.Zh);
        }
    }

    [Fact]
    public void The_language_preference_round_trips_and_falls_back_on_garbage()
    {
        using var fixture = new TemporaryDirectory();
        Assert.Equal(AppLanguage.Zh, DesktopLanguagePreference.Load(fixture.Path, AppLanguage.Zh));
        Assert.Equal(AppLanguage.En, DesktopLanguagePreference.Load(fixture.Path, AppLanguage.En));

        DesktopLanguagePreference.Save(fixture.Path, AppLanguage.En);
        Assert.Equal(AppLanguage.En, DesktopLanguagePreference.Load(fixture.Path, AppLanguage.Zh));

        string path = Path.Combine(fixture.Path, "runtime", "desktop_language.txt");
        File.WriteAllText(path, "unknown");
        Assert.Equal(AppLanguage.Zh, DesktopLanguagePreference.Load(fixture.Path, AppLanguage.Zh));
    }

    [Fact]
    public void The_system_default_follows_the_ui_culture_language()
    {
        AppLanguage expected = CultureInfo.CurrentUICulture.TwoLetterISOLanguageName.Equals(
            "zh", StringComparison.OrdinalIgnoreCase)
            ? AppLanguage.Zh
            : AppLanguage.En;
        Assert.Equal(expected, DesktopLanguagePreference.SystemDefault());
    }
    [Fact]
    public void The_markup_binding_updates_an_existing_control_after_a_switch()
    {
        Exception? failure = null;
        var thread = new Thread(() =>
        {
            System.Windows.Controls.TextBlock? block = null;
            try
            {
                Loc.Instance.SetLanguage(AppLanguage.Zh);
                block = new System.Windows.Controls.TextBlock();
                System.Windows.Data.BindingOperations.SetBinding(
                    block,
                    System.Windows.Controls.TextBlock.TextProperty,
                    new System.Windows.Data.Binding(".")
                    {
                        Source = Loc.Instance,
                        Mode = System.Windows.Data.BindingMode.OneWay,
                        Converter = TranslationConverter.Instance,
                        ConverterParameter = "切换EN/CN",
                    });
                TranslationBindingRegistry.Register(block, System.Windows.Controls.TextBlock.TextProperty);
                block.Dispatcher.Invoke(() => { }, System.Windows.Threading.DispatcherPriority.Background);
                Assert.Equal("切换EN/CN", block.Text);
                Loc.Instance.SetLanguage(AppLanguage.En);
                block.Dispatcher.Invoke(() => { }, System.Windows.Threading.DispatcherPriority.Background);
                Assert.Equal("Switch EN/CN", block.Text);
            }
            catch (Exception ex)
            {
                failure = ex;
            }
            finally
            {
                if (block != null)
                {
                    TranslationBindingRegistry.Unregister(block, System.Windows.Controls.TextBlock.TextProperty);
                }
                Loc.Instance.SetLanguage(AppLanguage.Zh);
            }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        Assert.True(thread.Join(TimeSpan.FromSeconds(15)), "STA binding test timed out");
        if (failure != null) throw failure;
    }
}
