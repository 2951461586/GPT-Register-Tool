// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// The account pool grid's state: every loaded row, the search / scope
    /// filter, the sort, the page window and the selected row. Window-independent
    /// so filtering and paging are xunit-testable (placement rule 8); loading
    /// rows from the backend and the grid's own selection widgets stay in the view.
    /// </summary>
    public sealed class PoolViewModel : ObservableObject
    {
        public const int DefaultPageSize = 100;
        public const int MaxPageSize = 500;

        private string searchText = "";
        private string pageSizeText = DefaultPageSize.ToString(CultureInfo.InvariantCulture);
        private object scopeFilter = "全部";
        private string pageStatusText = "第 0/0 页";
        private PoolRow? selectedRow;
        private string sortMember = "";
        private ListSortDirection? sortDirection;

        public PoolViewModel()
        {
            FirstPageCommand = new RelayCommand(() => GoToPage(1));
            PreviousPageCommand = new RelayCommand(() => GoToPage(CurrentPage - 1));
            NextPageCommand = new RelayCommand(() => GoToPage(CurrentPage + 1));
            LastPageCommand = new RelayCommand(GoToLastPage);
            ClearSearchCommand = new RelayCommand(() => SearchText = "");
            SelectAllFilteredCommand = new RelayCommand(CheckAllFiltered);
            ClearSelectionCommand = new RelayCommand(ClearSelection);
        }

        public ObservableCollection<PoolRow> AllRows { get; } = new ObservableCollection<PoolRow>();

        public ObservableCollection<PoolRow> PagedRows { get; } = new ObservableCollection<PoolRow>();

        public RelayCommand FirstPageCommand { get; }
        public RelayCommand PreviousPageCommand { get; }
        public RelayCommand NextPageCommand { get; }
        public RelayCommand LastPageCommand { get; }
        public RelayCommand ClearSearchCommand { get; }
        public RelayCommand SelectAllFilteredCommand { get; }
        public RelayCommand ClearSelectionCommand { get; }

        public int CurrentPage { get; set; } = 1;

        /// <summary>Rows matching the filter, before paging.</summary>
        public int FilteredCount { get; private set; }

        /// <summary>Raised after <see cref="PagedRows"/> was rebuilt.</summary>
        public event EventHandler? PageRefreshed;

        /// <summary>Raised after <see cref="ClearSelectionCommand"/> so the view can reset its grid widget.</summary>
        public event EventHandler? SelectionCleared;

        public string SearchText
        {
            get => searchText;
            set { searchText = value ?? ""; OnPropertyChanged(); ResetToFirstPage(); }
        }

        public string PageSizeText
        {
            get => pageSizeText;
            set { pageSizeText = value ?? "100"; OnPropertyChanged(); ResetToFirstPage(); }
        }

        public object ScopeFilter
        {
            get => scopeFilter;
            set { scopeFilter = value; OnPropertyChanged(); ResetToFirstPage(); }
        }

        public string PageStatusText
        {
            get => pageStatusText;
            private set { pageStatusText = value ?? ""; OnPropertyChanged(); }
        }

        // Null means "no row selected". Every reader starts from SelectedRow
        // and falls back to the grid's own selection.
        public PoolRow? SelectedRow
        {
            get => selectedRow;
            set => SetProperty(ref selectedRow, value);
        }

        public string SummaryText => $"共 {AllRows.Count} 条；当前筛选 {FilteredCount} 条";

        public int PageSizeValue()
        {
            return int.TryParse(PageSizeText, out int value) && value > 0 ? Math.Min(value, MaxPageSize) : DefaultPageSize;
        }

        public bool Matches(PoolRow? row)
        {
            if (row == null) return false;
            string scope = ScopeText(ScopeFilter);
            string term = (SearchText ?? "").Trim().ToLowerInvariant();

            if (scope == "有试用" && !PromotionStatusPresentation.IsTrialEligible(row.PromotionStatus, row.PromotionState)) return false;
            if (scope == "待处理" && !RegistrationStatusPresentation.NeedsAttention(row)) return false;
            if (term.Length == 0) return true;

            string text = (row.Identifier + " " + row.AccountType + " " + row.Status + " " + row.Notes).ToLowerInvariant();
            return text.Contains(term);
        }

        public List<PoolRow> FilteredRows() => AllRows.Where(Matches).ToList();

        public List<PoolRow> CheckedRows() => AllRows.Where(r => r.IsChecked).ToList();

        public void CheckAllFiltered()
        {
            foreach (PoolRow row in AllRows.Where(Matches)) row.IsChecked = true;
        }

        public void UncheckAll()
        {
            foreach (PoolRow row in AllRows) row.IsChecked = false;
        }

        public void ClearSelection()
        {
            UncheckAll();
            SelectedRow = null;
            SelectionCleared?.Invoke(this, EventArgs.Empty);
        }

        public void SortBy(string member, ListSortDirection direction)
        {
            sortMember = member;
            sortDirection = direction;
            ResetToFirstPage();
        }

        /// <summary>
        /// Rebuilds <see cref="PagedRows"/> from the filter, sort and page window.
        /// An out-of-range <see cref="CurrentPage"/> is clamped, so a page restored
        /// after the row count shrank stays valid.
        /// </summary>
        public void RefreshPagedRows()
        {
            var filtered = AccountGridOrdering.Apply(
                AllRows.Where(Matches),
                sortMember,
                sortDirection).ToList();
            FilteredCount = filtered.Count;
            int pageSize = PageSizeValue();
            int pageCount = Math.Max(1, (int)Math.Ceiling(FilteredCount / (double)pageSize));
            if (CurrentPage < 1) CurrentPage = 1;
            if (CurrentPage > pageCount) CurrentPage = pageCount;

            PagedRows.Clear();
            foreach (PoolRow row in filtered.Skip((CurrentPage - 1) * pageSize).Take(pageSize))
            {
                PagedRows.Add(row);
            }

            int start = FilteredCount == 0 ? 0 : (CurrentPage - 1) * pageSize + 1;
            int end = FilteredCount == 0 ? 0 : Math.Min(FilteredCount, CurrentPage * pageSize);
            PageStatusText = $"第 {CurrentPage}/{pageCount} 页，显示 {start}-{end} / {FilteredCount}";
            PageRefreshed?.Invoke(this, EventArgs.Empty);
        }

        private void ResetToFirstPage()
        {
            CurrentPage = 1;
            RefreshPagedRows();
        }

        private void GoToPage(int page)
        {
            CurrentPage = page;
            RefreshPagedRows();
        }

        private void GoToLastPage()
        {
            int count = AllRows.Count(Matches);
            GoToPage(Math.Max(1, (int)Math.Ceiling(count / (double)PageSizeValue())));
        }

        private static string ScopeText(object value)
        {
            if (value is ComboBoxItem item) return Convert.ToString(item.Content) ?? "";
            return Convert.ToString(value) ?? "";
        }
    }
}
