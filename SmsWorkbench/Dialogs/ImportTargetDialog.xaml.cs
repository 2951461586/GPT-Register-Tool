namespace SmsWorkbench.Dialogs
{
    public partial class ImportTargetDialog : Window
    {
        public string SelectedTarget { get; private set; } = string.Empty;

        public ImportTargetDialog(Window owner, string title)
        {
            InitializeComponent();
            WindowThemeService.Attach(this);
            Owner = owner;
            Title = title;
        }

        private void Ok_Click(object sender, RoutedEventArgs e)
        {
            SelectedTarget = ((TargetComboBox.SelectedItem as ComboBoxItem)?.Tag as string) ?? "cpa";
            DialogResult = true;
            Close();
        }

        private void Cancel_Click(object sender, RoutedEventArgs e)
        {
            SelectedTarget = string.Empty;
            DialogResult = false;
            Close();
        }
    }
}
