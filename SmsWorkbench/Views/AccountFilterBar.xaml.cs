namespace SmsWorkbench.Views
{
    /// <summary>
    /// Account KPI metric strip plus the search / scope / page-size / paging bar.
    ///
    /// Inherits MainWindow's DataContext so every binding keeps its <c>Pool.*</c>
    /// path. Clicking a filterable KPI tile routes directly into
    /// <see cref="PoolViewModel.ScopeFilter"/> without duplicating filter state.
    /// </summary>
    public partial class AccountFilterBar : UserControl
    {
        public AccountFilterBar()
        {
            InitializeComponent();
        }

        private void KpiScopeTile_Click(object sender, RoutedEventArgs e)
        {
            if (sender is Button { Tag: string scopeKey } && DataContext is MainWindow window)
            {
                window.Pool.ScopeFilter = scopeKey;
            }
        }
    }
}
