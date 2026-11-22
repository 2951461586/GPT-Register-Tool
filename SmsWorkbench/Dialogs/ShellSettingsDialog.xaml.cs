using SmsWorkbench.Localization;

namespace SmsWorkbench.Dialogs
{
    public partial class ShellSettingsDialog : Window
    {
        public DesktopShellPreferences? SelectedPreferences { get; private set; }

        public ShellSettingsDialog(Window owner, DesktopShellPreferences current, string hotkeyLabel)
        {
            InitializeComponent();
            WindowThemeService.Attach(this);
            Owner = owner;
            TrayCheckBox.IsChecked = current.Tray;
            HotkeyCheckBox.IsChecked = current.Hotkey;
            HotkeyCheckBox.Content = Loc.F("启用全局热键 {0}", hotkeyLabel);
        }

        private void CancelButton_Click(object sender, RoutedEventArgs e)
        {
            DialogResult = false;
            Close();
        }

        private void SaveButton_Click(object sender, RoutedEventArgs e)
        {
            SelectedPreferences = new DesktopShellPreferences(
                Tray: TrayCheckBox.IsChecked == true,
                Hotkey: HotkeyCheckBox.IsChecked == true);
            DialogResult = true;
            Close();
        }
    }
}
