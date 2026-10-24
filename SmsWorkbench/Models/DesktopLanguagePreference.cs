// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// Persists the desktop language next to the theme preference
    /// (<c>runtime/desktop_language.txt</c>, <c>CN</c> or <c>EN</c>).
    ///
    /// First launch follows the OS UI language: a Chinese system starts in
    /// Chinese, everything else starts in English. Once the operator toggles the
    /// language, the saved value wins.
    /// </summary>
    internal static class DesktopLanguagePreference
    {
        private const string FileName = "desktop_language.txt";

        private static string LanguagePath(string rootDirectory)
            => Path.Combine(rootDirectory, "runtime", FileName);

        public static AppLanguage SystemDefault()
            => CultureInfo.CurrentUICulture.TwoLetterISOLanguageName.Equals("zh", StringComparison.OrdinalIgnoreCase)
                ? AppLanguage.Zh
                : AppLanguage.En;

        public static AppLanguage Load(string rootDirectory, AppLanguage fallback)
        {
            try
            {
                return File.ReadAllText(LanguagePath(rootDirectory), Encoding.UTF8).Trim().ToUpperInvariant() switch
                {
                    "EN" => AppLanguage.En,
                    "CN" => AppLanguage.Zh,
                    _ => fallback,
                };
            }
            catch (IOException)
            {
                return fallback;
            }
            catch (UnauthorizedAccessException)
            {
                return fallback;
            }
        }

        public static void Save(string rootDirectory, AppLanguage language)
        {
            try
            {
                string path = LanguagePath(rootDirectory);
                string? directory = Path.GetDirectoryName(path);
                if (!string.IsNullOrEmpty(directory))
                {
                    Directory.CreateDirectory(directory);
                }
                File.WriteAllText(path, language == AppLanguage.En ? "EN" : "CN", new UTF8Encoding(false));
            }
            catch (IOException)
            {
                // A read-only install directory must not break the UI toggle.
            }
            catch (UnauthorizedAccessException)
            {
            }
        }
    }
}
