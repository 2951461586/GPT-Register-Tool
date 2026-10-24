// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// The EN/CN toggle in the sidebar. The WPF-owned UI is bilingual; the
    /// Python backend's CLI output and log lines stay Chinese on purpose (see
    /// `docs/architecture.md`), so the in-app log transcript is not translated.
    /// </summary>
    public partial class MainWindow
    {
        /// <summary>"EN" in the Chinese build, "中文" in the English build: the language the button switches *to*.</summary>
        public string LanguageButtonText => Loc.Instance.IsEnglish ? "中文" : "EN";

        private void ToggleLanguage()
        {
            Loc.Instance.Toggle();
            DesktopLanguagePreference.Save(rootDir, Loc.Instance.Language);
            OnPropertyChanged(nameof(LanguageButtonText));
            // View models cache composed display strings; let every one of them
            // re-read Loc.T after the switch instead of showing a mixed UI.
            Pool?.RefreshLanguage();
            StatusBar?.RefreshLanguage();
            TaskList?.RefreshLanguage();
            // Row cells are precomputed display strings (status / AT / RT /
            // 2FA / promotion), so the grid has to be re-projected to pick up
            // the new language. Preserve the operator's scroll/selection.
            bool fullReload = true;
            RefreshPoolsPreservingView(forceFull: fullReload);
            Log($"语言已切换为 {Loc.Instance.Language}");
        }
    }
}
