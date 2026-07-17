namespace SmsWorkbench
{
    public partial class MainWindow
    {
        private async void ShowThemedInfoDialog(string title, string message)
        {
            await DialogFactory.ShowInfoAsync(this, title, message);
        }
    }
}
