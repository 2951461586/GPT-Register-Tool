namespace SmsWorkbench;

internal sealed record ChangeEmailDialogOptions(
    string Provider,
    int Workers,
    string MailboxFile,
    string SmailrDomain,
    string CfworkerDomain);

internal static class ChangeEmailDialogService
{
    private static readonly (string Label, string Value)[] Providers =
    {
        ("ReMail", "remail"),
        ("CF Worker 域名邮箱", "cfworker"),
        ("Smailr", "smailr"),
        ("iCloud 邮箱池", "icloud"),
        ("Outlook 邮箱池", "outlook"),
        ("Hotmail 邮箱池", "hotmail"),
    };

    public static ChangeEmailDialogOptions? Show(
        Window owner,
        int count,
        int defaultWorkers,
        string smailrDomain,
        string cfworkerDomain)
    {
        int selectedWorkers = Math.Min(defaultWorkers, Math.Max(1, count));
        var providerBox = new ComboBox { Width = 260, Margin = new Thickness(0, 0, 0, 10) };
        foreach (var provider in Providers)
        {
            providerBox.Items.Add(new ComboBoxItem { Content = provider.Label, Tag = provider.Value });
        }
        providerBox.SelectedIndex = 0;

        var workerBox = new TextBox
        {
            Text = selectedWorkers.ToString(System.Globalization.CultureInfo.InvariantCulture),
            Width = 260,
            Margin = new Thickness(0, 0, 0, 10),
        };
        var fileBox = new TextBox { Width = 210, Margin = new Thickness(0, 0, 8, 10) };
        var browse = new Button { Content = "选择凭证文件", Margin = new Thickness(0, 0, 0, 10) };
        browse.Click += (_, _) =>
        {
            var dialog = new Microsoft.Win32.OpenFileDialog
            {
                Filter = "文本文件 (*.txt)|*.txt|所有文件 (*.*)|*.*",
            };
            if (dialog.ShowDialog() == true)
            {
                fileBox.Text = dialog.FileName;
            }
        };

        var root = new StackPanel { Margin = new Thickness(20) };
        root.Children.Add(new TextBlock
        {
            Text = $"目标邮箱 provider（{count} 个账号）",
            Margin = new Thickness(0, 0, 0, 6),
        });
        root.Children.Add(providerBox);
        root.Children.Add(new TextBlock { Text = "并发数" });
        root.Children.Add(workerBox);
        root.Children.Add(new TextBlock { Text = "iCloud/Outlook/Hotmail 需提供等量凭证文件" });

        var fileRow = new StackPanel { Orientation = Orientation.Horizontal };
        fileRow.Children.Add(fileBox);
        fileRow.Children.Add(browse);
        root.Children.Add(fileRow);

        var actions = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right,
        };
        var ok = new Button { Content = "开始", Width = 80, IsDefault = true };
        var cancel = new Button
        {
            Content = "取消",
            Width = 80,
            IsCancel = true,
            Margin = new Thickness(8, 0, 0, 0),
        };
        actions.Children.Add(ok);
        actions.Children.Add(cancel);
        root.Children.Add(actions);

        var dialogWindow = DialogFactory.Create(
            owner,
            "邮箱换绑",
            460,
            360,
            minWidth: 440,
            minHeight: 340,
            resizeMode: ResizeMode.NoResize);
        dialogWindow.Content = root;

        ChangeEmailDialogOptions? selected = null;
        ok.Click += (_, _) =>
        {
            string provider = (providerBox.SelectedItem as ComboBoxItem)?.Tag as string ?? "";
            if (string.IsNullOrWhiteSpace(provider))
            {
                return;
            }

            selectedWorkers = ParsePositiveInt(workerBox.Text, 1, 16, selectedWorkers);
            selected = new ChangeEmailDialogOptions(
                provider,
                selectedWorkers,
                fileBox.Text.Trim(),
                smailrDomain ?? "",
                cfworkerDomain ?? "");
            dialogWindow.DialogResult = true;
        };
        dialogWindow.ShowDialog();
        return selected;
    }

    private static int ParsePositiveInt(string value, int minimum, int maximum, int fallback)
    {
        return int.TryParse(value, out int parsed)
            ? Math.Clamp(parsed, minimum, maximum)
            : fallback;
    }
}
