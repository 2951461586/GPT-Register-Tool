namespace SmsWorkbench.Dialogs;

internal sealed record ChangeEmailDialogOptions(
    string Provider,
    int Workers,
    string MailboxFile,
    string SmailrDomain,
    string CfworkerDomain);

internal static class ChangeEmailDialogService
{
    public static ChangeEmailDialogOptions? Show(
        Window owner,
        int count,
        int defaultWorkers,
        string smailrDomain,
        string cfworkerDomain)
    {
        var dialog = new ChangeEmailDialog(owner, count, defaultWorkers, smailrDomain, cfworkerDomain);
        return dialog.ShowDialog() == true ? dialog.SelectedOptions : null;
    }
}
