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

        /// <summary>
        /// True when <paramref name="text"/> carries the display form of
        /// <paramref name="marker"/> in <b>any</b> language this build ships.
        ///
        /// For <i>decisions</i>, never for display. Rendering a row through
        /// <c>Loc.T</c> makes its text follow the active language, so a predicate
        /// that compares against the Chinese literal silently stops matching once
        /// the operator switches to English -- the row is then classified
        /// differently by the same build. Measured 2026-10-25: in EN the
        /// registered counter dropped and already-registered mailboxes were
        /// offered for registration again.
        ///
        /// The result deliberately does not depend on the current language: the
        /// Chinese source form and every translation are checked together, so a
        /// row built in one language stays classified the same way after a
        /// switch (and in tests, without mutating the global language).
        ///
        /// <para>
        /// The marker is matched against the catalog **both ways** (see
        /// <see cref="DisplayFormsFor"/>). Matching only keys that *begin* with it
        /// reintroduced the same language asymmetry on the other side: the Chinese
        /// row text ``网络失败`` contains ``失败`` and needed attention, while its
        /// English form ``Network failure`` contains no ``Failed`` and silently did
        /// not. Measured 2026-10-26 on the two probe-failure statuses
        /// (``network_failed`` / ``mailbox_failed``).
        /// </para>
        /// </summary>
        public static bool DisplayContains(string? text, string marker)
        {
            if (string.IsNullOrEmpty(text) || string.IsNullOrEmpty(marker)) return false;
            foreach (string form in DisplayFormsFor(marker))
            {
                // Case-insensitive so the English forms match regardless of how
                // the backend cased a label ("Registered" vs "registered").
                if (text.Contains(form, StringComparison.OrdinalIgnoreCase)) return true;
            }
            return false;
        }

        // Marker -> every display form that marker can appear as, across all
        // languages. Built on first use because the marker set is small (a
        // handful of status predicates) while the catalog has hundreds of keys;
        // per-row calls then cost a few ``Contains`` instead of a full scan.
        private static readonly Dictionary<string, string[]> DisplayForms = new(StringComparer.Ordinal);
        private static readonly object DisplayFormsLock = new();

        private static string[] DisplayFormsFor(string marker)
        {
            lock (DisplayFormsLock)
            {
                if (DisplayForms.TryGetValue(marker, out string[]? cached)) return cached;

                var forms = new List<string> { marker };
                foreach (KeyValuePair<string, string> pair in StringsEn.Map)
                {
                    // Matching is deliberately **symmetric**:
                    //
                    // * ``StartsWith`` covers a marker that names a label family
                    //   (``已导入`` for 已导入CPA/SUB2).
                    // * ``EndsWith`` covers a marker that is a *qualifier* the row
                    //   text appends (``失败`` in 网络失败 / 邮箱链路失败). Without
                    //   it only the Chinese literal matched, because the English
                    //   translations of those keys ("Network failure", "Mailbox
                    //   failure") do not contain "Failed".
                    //
                    // Neither direction can over-reach: a translation only becomes
                    // a form if its key really starts or ends with the marker, and
                    // the form is then a full label ("Network failure"), not a
                    // fragment -- a long sentence key is harmless because a status
                    // cell never contains one.
                    if (pair.Key.StartsWith(marker, StringComparison.Ordinal)
                        || pair.Key.EndsWith(marker, StringComparison.Ordinal))
                    {
                        if (!string.IsNullOrEmpty(pair.Value))
                        {
                            forms.Add(pair.Value);
                        }
                    }
                }
                string[] result = forms.ToArray();
                DisplayForms[marker] = result;
                return result;
            }
        }

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
