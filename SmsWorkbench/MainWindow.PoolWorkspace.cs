namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Pool workspace state: row cache, paging, sorting, selection and the
        // refresh-in-flight flag. Extracted from the MainWindow god-class so the
        // pool/session UI surface has a single obvious home. References from the
        // other MainWindow.* partials stay valid because partials share one class
        // scope, so no caller needed rewriting.
        private readonly ObservableCollection<PoolRow> allRows = new ObservableCollection<PoolRow>();

        public ObservableCollection<PoolRow> PagedRows { get; } = new ObservableCollection<PoolRow>();

        private int currentPage = 1;
        private int filteredCount;
        private string accountSortMember = "";
        private ListSortDirection? accountSortDirection;
        public PoolRow SelectedRow { get; set; }
        private bool poolsRefreshRunning;
    }
}
