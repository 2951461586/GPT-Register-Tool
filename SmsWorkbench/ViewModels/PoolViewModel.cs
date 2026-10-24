// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// The account pool grid's state: every loaded row, the search / scope
    /// filter, the sort, the page window and the selected row. Window-independent
    /// so filtering and paging are xunit-testable (placement rule 8); loading
    /// rows from the backend and the grid's own selection widgets stay in the view.
    /// </summary>
    /// <summary>Stable filter key plus the localized label the combo box shows.</summary>
    public sealed record ScopeOption(string Key, string Label);

    public sealed class PoolViewModel : ObservableObject
    {
        public const int DefaultPageSize = 100;
        public const int MaxPageSize = 500;

        private string searchText = "";
        private string pageSizeText = DefaultPageSize.ToString(CultureInfo.InvariantCulture);
        private object scopeFilter = "all";
        private string pageStatusText = "第 0/0 页";
        private string totalCountText = "0";
        private string trialCountText = "0";
        private string registeredCountText = "0";
        private string attentionCountText = "0";
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
            set
            {
                searchText = value ?? "";
                OnPropertyChanged();
                // Drives the clear (×) button in AccountFilterBar; it used to be
                // toggled by name from MainWindow's code-behind.
                OnPropertyChanged(nameof(HasSearchText));
                ResetToFirstPage();
            }
        }

        /// <summary>True when the search box contains anything; the clear button binds to this.</summary>
        public bool HasSearchText => searchText.Length > 0;

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

        /// <summary>
        /// Filter options carry a stable key (all/trial/attention) and a localized
        /// label. The key is what <see cref="Matches"/> compares, so switching the
        /// language can never change which rows a filter selects.
        /// </summary>
        public IReadOnlyList<ScopeOption> ScopeOptions => new[]
        {
            new ScopeOption("all", Loc.T("全部")),
            new ScopeOption("trial", Loc.T("有试用")),
            new ScopeOption("attention", Loc.T("待处理")),
        };

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

        public string TotalCountText
        {
            get => totalCountText;
            private set => SetProperty(ref totalCountText, value);
        }

        public string TrialCountText
        {
            get => trialCountText;
            private set => SetProperty(ref trialCountText, value);
        }

        public string RegisteredCountText
        {
            get => registeredCountText;
            private set => SetProperty(ref registeredCountText, value);
        }

        public string AttentionCountText
        {
            get => attentionCountText;
            private set => SetProperty(ref attentionCountText, value);
        }

        /// <summary>Recomputes the overview counters shown above the grid.</summary>
        public void UpdateOverview()
        {
            TotalCountText = AllRows.Count.ToString(CultureInfo.InvariantCulture);
            TrialCountText = AllRows
                .Count(r => PromotionStatusPresentation.IsTrialEligible(r.PromotionStatus, r.PromotionState))
                .ToString(CultureInfo.InvariantCulture);
            RegisteredCountText = AllRows.Count(IsRegistered).ToString(CultureInfo.InvariantCulture);
            AttentionCountText = AllRows.Count(RegistrationStatusPresentation.NeedsAttention).ToString(CultureInfo.InvariantCulture);
        }

        public static bool IsRegistered(PoolRow row)
        {
            if (RegistrationStatusPresentation.IsPartial(row.RegistrationStatus)
                || RegistrationStatusPresentation.IsPartial(row.Status)) return false;
            return row.AccountType.Contains("Session")
                || row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase)
                || row.Status.Contains("已注册")
                || row.Status.Contains("PayPal");
        }

        public string SummaryText => Loc.F("共 {0} 条；当前筛选 {1} 条", AllRows.Count, FilteredCount);

        /// <summary>Rebuilds the language-dependent composite strings after a switch.</summary>
        public void RefreshLanguage()
        {
            OnPropertyChanged(string.Empty);
            RefreshPagedRows();
        }

        public int PageSizeValue()
        {
            return int.TryParse(PageSizeText, out int value) && value > 0 ? Math.Min(value, MaxPageSize) : DefaultPageSize;
        }

        public bool Matches(PoolRow? row)
        {
            if (row == null) return false;
            string scope = ScopeText(ScopeFilter);
            string term = (SearchText ?? "").Trim().ToLowerInvariant();

            if (scope == "trial" && !PromotionStatusPresentation.IsTrialEligible(row.PromotionStatus, row.PromotionState)) return false;
            if (scope == "attention" && !RegistrationStatusPresentation.NeedsAttention(row)) return false;
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
        /// Applies one incremental accounts refresh to <see cref="AllRows"/>.
        /// </summary>
        /// <remarks>
        /// Only rows owned by <paramref name="databasePath"/> are touched: the
        /// mailbox-pool rows in the same collection come from a different read
        /// (the pool file), and the incremental path deliberately does not
        /// re-read that file. A row absent from <paramref name="currentIds"/> was
        /// deleted by another process and is dropped; a matching row is replaced
        /// in place so the operator's checkbox survives; a new id is appended.
        /// </remarks>
        public void MergeDatabaseRows(
            IReadOnlyList<PoolRow> changed,
            IReadOnlySet<string> currentIds,
            string databasePath)
        {
            bool Owned(PoolRow row) =>
                string.Equals(row.SourcePath, databasePath, StringComparison.OrdinalIgnoreCase);

            foreach (PoolRow row in AllRows.Where(Owned).Where(row => !currentIds.Contains(row.RawLine)).ToList())
            {
                AllRows.Remove(row);
            }

            foreach (PoolRow fresh in changed)
            {
                int index = -1;
                for (int i = 0; i < AllRows.Count; i++)
                {
                    if (Owned(AllRows[i]) && AllRows[i].RawLine == fresh.RawLine)
                    {
                        index = i;
                        break;
                    }
                }
                if (index < 0)
                {
                    AllRows.Add(fresh);
                    continue;
                }
                fresh.IsChecked = AllRows[index].IsChecked;
                AllRows[index] = fresh;
            }
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
            if (value is ComboBoxItem item) return Convert.ToString(item.Content, CultureInfo.InvariantCulture) ?? "";
            return Convert.ToString(value, CultureInfo.InvariantCulture) ?? "";
        }
    }
}
