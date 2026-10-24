using System.ComponentModel;
using System.Linq;
using SmsWorkbench;
using Xunit;

namespace SmsWorkbench.Tests
{
    public class PoolViewModelTests
    {
        private static PoolViewModel WithRows(int count, string pageSize = "10")
        {
            var pool = new PoolViewModel { PageSizeText = pageSize };
            for (int i = 1; i <= count; i++)
            {
                pool.AllRows.Add(new PoolRow { Identifier = $"user{i:000}@example.com", Status = "已注册" });
            }
            pool.RefreshPagedRows();
            return pool;
        }

        [Theory]
        [InlineData("abc", 100)]
        [InlineData("0", 100)]
        [InlineData("-5", 100)]
        [InlineData("25", 25)]
        [InlineData("9999", 500)]
        public void PageSizeValue_FallsBackAndClamps(string text, int expected)
        {
            var pool = new PoolViewModel { PageSizeText = text };

            Assert.Equal(expected, pool.PageSizeValue());
        }

        [Fact]
        public void RefreshPagedRows_ShowsOnlyTheCurrentPage()
        {
            var pool = WithRows(25);

            Assert.Equal(10, pool.PagedRows.Count);
            Assert.Equal("第 1/3 页，显示 1-10 / 25", pool.PageStatusText);

            pool.NextPageCommand.Execute(null);
            pool.NextPageCommand.Execute(null);

            Assert.Equal(5, pool.PagedRows.Count);
            Assert.Equal("第 3/3 页，显示 21-25 / 25", pool.PageStatusText);
        }

        [Fact]
        public void PageNavigation_ClampsAtBothEnds()
        {
            var pool = WithRows(25);

            pool.PreviousPageCommand.Execute(null);
            Assert.Equal(1, pool.CurrentPage);

            pool.LastPageCommand.Execute(null);
            Assert.Equal(3, pool.CurrentPage);

            pool.NextPageCommand.Execute(null);
            Assert.Equal(3, pool.CurrentPage);

            pool.FirstPageCommand.Execute(null);
            Assert.Equal(1, pool.CurrentPage);
        }

        [Fact]
        public void EmptyPool_ReportsAnEmptyWindow()
        {
            var pool = WithRows(0);

            Assert.Empty(pool.PagedRows);
            Assert.Equal("第 1/1 页，显示 0-0 / 0", pool.PageStatusText);
        }

        [Fact]
        public void SearchText_FiltersCaseInsensitivelyAndResetsToPageOne()
        {
            var pool = WithRows(25);
            pool.LastPageCommand.Execute(null);

            pool.SearchText = "  USER007 ";

            Assert.Equal(1, pool.CurrentPage);
            Assert.Equal(1, pool.FilteredCount);
            Assert.Equal("user007@example.com", pool.PagedRows.Single().Identifier);
            Assert.Equal("共 25 条；当前筛选 1 条", pool.SummaryText);
        }

        [Fact]
        public void ScopeFilter_AttentionKeepsOnlyRowsNeedingAction()
        {
            var pool = WithRows(3);
            pool.AllRows[1].Status = "待支付";

            pool.ScopeFilter = "attention";

            Assert.Equal(new[] { "user002@example.com" }, pool.PagedRows.Select(r => r.Identifier));
        }

        [Fact]
        public void SortBy_OrdersTheFilteredRowsAndResetsThePage()
        {
            var pool = WithRows(15);
            pool.NextPageCommand.Execute(null);

            pool.SortBy(nameof(PoolRow.Identifier), ListSortDirection.Descending);

            Assert.Equal(1, pool.CurrentPage);
            Assert.Equal("user015@example.com", pool.PagedRows.First().Identifier);
        }

        [Fact]
        public void RefreshPagedRows_ClampsAPageBeyondTheShrunkenPool()
        {
            var pool = WithRows(25);
            pool.LastPageCommand.Execute(null);
            while (pool.AllRows.Count > 5) pool.AllRows.RemoveAt(pool.AllRows.Count - 1);

            pool.RefreshPagedRows();

            Assert.Equal(1, pool.CurrentPage);
            Assert.Equal(5, pool.PagedRows.Count);
        }

        [Fact]
        public void CheckAllFiltered_OnlyTouchesMatchingRows()
        {
            var pool = WithRows(5);
            pool.SearchText = "user001";

            pool.CheckAllFiltered();

            Assert.Equal(new[] { "user001@example.com" }, pool.CheckedRows().Select(r => r.Identifier));

            pool.UncheckAll();
            Assert.Empty(pool.CheckedRows());
        }

        [Fact]
        public void ClearSearchCommand_RestoresTheFullList()
        {
            var pool = WithRows(5);
            pool.SearchText = "user003";
            Assert.Equal(1, pool.FilteredCount);

            pool.ClearSearchCommand.Execute(null);

            Assert.Equal("", pool.SearchText);
            Assert.Equal(5, pool.FilteredCount);
        }

        [Fact]
        public void ClearSelectionCommand_UnchecksRowsDropsTheSelectionAndSignalsTheView()
        {
            var pool = WithRows(3);
            pool.SelectAllFilteredCommand.Execute(null);
            pool.SelectedRow = pool.AllRows[0];
            int cleared = 0;
            pool.SelectionCleared += (_, _) => cleared++;

            pool.ClearSelectionCommand.Execute(null);

            Assert.Empty(pool.CheckedRows());
            Assert.Null(pool.SelectedRow);
            Assert.Equal(1, cleared);
        }

        [Fact]
        public void UpdateOverview_CountsTotalTrialRegisteredAndAttention()
        {
            var pool = new PoolViewModel();
            pool.AllRows.Add(new PoolRow { Identifier = "a", Status = "已注册" });
            pool.AllRows.Add(new PoolRow { Identifier = "b", Status = "待支付", PromotionState = "trial_eligible" });
            pool.AllRows.Add(new PoolRow { Identifier = "c", Status = "邮箱可用" });
            var changed = new System.Collections.Generic.List<string?>();
            pool.PropertyChanged += (_, e) => changed.Add(e.PropertyName);

            pool.UpdateOverview();

            Assert.Equal("3", pool.TotalCountText);
            Assert.Equal("1", pool.RegisteredCountText);
            Assert.Equal("1", pool.AttentionCountText);
            Assert.Contains("TotalCountText", changed);
        }

        [Fact]
        public void PageRefreshed_FiresOnEveryRebuild()
        {
            var pool = WithRows(3);
            int fired = 0;
            pool.PageRefreshed += (_, _) => fired++;

            pool.RefreshPagedRows();
            pool.SearchText = "user";

            Assert.Equal(2, fired);
        }

        [Fact]
        public void SelectedRow_NotifiesBindings()
        {
            var pool = WithRows(1);
            var changed = new System.Collections.Generic.List<string?>();
            pool.PropertyChanged += (_, e) => changed.Add(e.PropertyName);

            pool.SelectedRow = pool.AllRows[0];
            pool.SelectedRow = null;

            Assert.Equal(new[] { "SelectedRow", "SelectedRow" }, changed);
        }

        private const string DatabasePath = "C:\\app\\runtime\\accounts.sqlite3";

        private static PoolRow DbRow(string id, string identifier, string status = "已注册")
            => new PoolRow { RawLine = id, SourcePath = DatabasePath, Identifier = identifier, Status = status };

        [Fact]
        public void MergeDatabaseRows_DropsDeletedRowsAndKeepsMailboxRows()
        {
            var pool = new PoolViewModel();
            pool.AllRows.Add(DbRow("1", "a@example.test"));
            pool.AllRows.Add(DbRow("2", "b@example.test"));
            pool.AllRows.Add(new PoolRow { RawLine = "pool-line", SourcePath = "C:\\app\\mailbox.txt", Identifier = "c@example.test" });

            pool.MergeDatabaseRows(
                System.Array.Empty<PoolRow>(),
                new System.Collections.Generic.HashSet<string> { "1" },
                DatabasePath);

            Assert.Equal(new[] { "1", "pool-line" }, pool.AllRows.Select(r => r.RawLine).ToArray());
        }

        [Fact]
        public void MergeDatabaseRows_ReplacesInPlaceAndKeepsTheCheckbox()
        {
            var pool = new PoolViewModel();
            pool.AllRows.Add(DbRow("1", "a@example.test", "old"));
            pool.AllRows[0].IsChecked = true;

            pool.MergeDatabaseRows(
                new[] { DbRow("1", "a@example.test", "new") },
                new System.Collections.Generic.HashSet<string> { "1" },
                DatabasePath);

            Assert.Single(pool.AllRows);
            Assert.Equal("new", pool.AllRows[0].Status);
            Assert.True(pool.AllRows[0].IsChecked);
        }

        [Fact]
        public void MergeDatabaseRows_AppendsNewIds()
        {
            var pool = new PoolViewModel();
            pool.AllRows.Add(DbRow("1", "a@example.test"));

            pool.MergeDatabaseRows(
                new[] { DbRow("2", "b@example.test") },
                new System.Collections.Generic.HashSet<string> { "1", "2" },
                DatabasePath);

            Assert.Equal(new[] { "1", "2" }, pool.AllRows.Select(r => r.RawLine).ToArray());
        }

        [Fact]
        public void MergeDatabaseRows_IgnoresRowsOwnedByAnotherSource()
        {
            var pool = new PoolViewModel();
            pool.AllRows.Add(new PoolRow { RawLine = "1", SourcePath = "C:\\other\\accounts.sqlite3", Identifier = "x@example.test" });

            pool.MergeDatabaseRows(
                System.Array.Empty<PoolRow>(),
                new System.Collections.Generic.HashSet<string>(),
                DatabasePath);

            Assert.Single(pool.AllRows);
        }
    }
}
