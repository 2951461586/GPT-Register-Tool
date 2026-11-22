using SmsWorkbench.Localization;

namespace SmsWorkbench.Dialogs
{
    public partial class ExportCompleteDialog : Window
    {
        private readonly string outputPath;
        private readonly Action<string> openPath;
        private readonly Func<string> runtimeDirectory;

        public ExportCompleteDialog(
            Window owner,
            string outputPath,
            int exportedCount,
            int skippedCount,
            string formatLabel,
            string formatDescription,
            Action<string> openPath,
            Func<string> runtimeDirectory)
        {
            this.outputPath = outputPath;
            this.openPath = openPath;
            this.runtimeDirectory = runtimeDirectory;

            InitializeComponent();
            WindowThemeService.Attach(this);
            Owner = owner;
            DescriptionTextBlock.Text = Loc.F("已生成账号 {0} 文件：{1}", formatLabel, formatDescription);
            CountsTextBlock.Text = Loc.F("数量：{0}    跳过：{1}", exportedCount, skippedCount);
            PathTextBlock.Text = outputPath;
        }

        private void OpenDir_Click(object sender, RoutedEventArgs e)
        {
            string directory = Path.GetDirectoryName(outputPath) ?? runtimeDirectory();
            openPath(directory);
            Close();
        }

        private void Close_Click(object sender, RoutedEventArgs e) => Close();
    }
}
