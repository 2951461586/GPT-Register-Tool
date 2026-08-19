namespace SmsWorkbench
{
    public partial class MainWindow
    {
        private void ShowThemedInfoDialog(string title, string message)
            => RunUiTask(() => DialogFactory.ShowInfoAsync(this, title, message));
    }
}
