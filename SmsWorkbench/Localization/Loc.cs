// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable


namespace SmsWorkbench.Localization
{
    public enum AppLanguage
    {
        Zh,
        En,
    }

    /// <summary>
    /// The single runtime translation point for the desktop UI.
    ///
    /// The Chinese source string is the key: Chinese is the authored copy and
    /// <see cref="StringsEn.Map"/> holds the English translation. That keeps the
    /// call sites readable (`Loc.T("一键注册")`) and means a missing translation
    /// falls back to the Chinese text instead of a raw key. The Python backend
    /// is deliberately not localized -- its CLI output and log lines stay Chinese
    /// (see docs/architecture.md), so only WPF-owned strings live here.
    /// </summary>
    public sealed class Loc : INotifyPropertyChanged
    {
        private static readonly Lazy<Loc> LazyInstance = new(() => new Loc());

        private AppLanguage language = AppLanguage.Zh;

        private Loc()
        {
        }

        public static Loc Instance => LazyInstance.Value;

        public AppLanguage Language => language;

        public bool IsEnglish => language == AppLanguage.En;

        public event PropertyChangedEventHandler? PropertyChanged;

        /// <summary>Raised after the language changed and every binding has been invalidated.</summary>
        public event EventHandler? LanguageChanged;

        /// <summary>Translates <paramref name="key"/> with the active language.</summary>
        public string Translate(string? key)
        {
            if (string.IsNullOrEmpty(key) || language == AppLanguage.Zh)
            {
                return key ?? "";
            }
            return StringsEn.Map.TryGetValue(key, out string? value) ? value : key;
        }

        /// <summary>Indexer form used by the <c>{loc:Tr}</c> markup extension binding.</summary>
        public string this[string key] => Translate(key);

        public static string T(string key) => Instance.Translate(key);

        /// <summary>Translates a template and formats it (templates use <c>{0}</c> placeholders).</summary>
        public static string F(string template, params object?[] args)
            => string.Format(CultureInfo.CurrentCulture, T(template), args);

        public void SetLanguage(AppLanguage value)
        {
            if (language == value)
            {
                return;
            }
            language = value;
            // An empty property name tells WPF to re-evaluate every binding on
            // this source, which is what the {loc:Tr ...} bindings listen for.
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(string.Empty));
            TranslationBindingRegistry.RefreshAll();
            LanguageChanged?.Invoke(this, EventArgs.Empty);
        }

        public void Toggle() => SetLanguage(language == AppLanguage.Zh ? AppLanguage.En : AppLanguage.Zh);
    }
}
