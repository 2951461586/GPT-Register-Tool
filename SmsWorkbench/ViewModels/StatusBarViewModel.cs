using SmsWorkbench.Localization;

namespace SmsWorkbench.ViewModels
{
    /// <summary>The one-line status shown at the bottom of the main window.</summary>
    public sealed class StatusBarViewModel : ObservableObject
    {
        private string text = Loc.T("就绪");

        public string Text
        {
            get => text;
            set => SetProperty(ref text, value ?? "");
        }

        /// <summary>Re-reads the localized idle copy after a language switch.</summary>
        public void RefreshLanguage()
        {
            OnPropertyChanged(nameof(Text));
        }
    }
}
