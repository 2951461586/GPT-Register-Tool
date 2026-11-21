using System.Windows.Input;

using SmsWorkbench.Localization;

namespace SmsWorkbench.Dialogs
{
    public partial class InboxDialogWindow : Window
    {
        private readonly Action refreshAction;
        private readonly Action<MailItem> openDetailAction;

        public ObservableCollection<MailItem> MailItems { get; } = new();

        public InboxDialogWindow(
            Window owner,
            string identifier,
            Action refreshAction,
            Action<MailItem> openDetailAction)
        {
            this.refreshAction = refreshAction;
            this.openDetailAction = openDetailAction;

            InitializeComponent();
            Owner = owner;
            Title = Loc.F("收件箱 - {0}", identifier);
            MailDataGrid.ItemsSource = MailItems;
        }

        public void SetHeaderText(string text)
        {
            HeaderTextBlock.Text = text;
        }

        private void RefreshButton_Click(object sender, RoutedEventArgs e)
        {
            refreshAction();
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e) => Close();

        private void MailDataGrid_MouseDoubleClick(object sender, MouseButtonEventArgs e)
        {
            if (MailDataGrid.SelectedItem is MailItem item)
            {
                openDetailAction(item);
            }
        }
    }
}
