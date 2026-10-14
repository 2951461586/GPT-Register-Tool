using SmsWorkbench;
using Xunit;

namespace SmsWorkbench.Tests
{
    public class TaskListViewModelTests
    {
        [Fact]
        public void StartBatch_NumbersRowsSequentiallyAndMarksThemRunning()
        {
            var list = new TaskListViewModel();

            TaskRow first = list.StartBatch("register", "--count 1");
            TaskRow second = list.StartBatch("scan", "--all");

            Assert.Equal("批次 1", first.Name);
            Assert.Equal("批次 2", second.Name);
            Assert.Equal("运行中", first.Status);
            Assert.Equal("register", first.Task);
            Assert.Equal("--count 1", first.Info);
            Assert.Equal(new[] { first, second }, list.Tasks);
        }

        [Fact]
        public void Last_IsNullWhenEmptyAndTracksTheNewestRow()
        {
            var list = new TaskListViewModel();
            Assert.Null(list.Last);

            list.StartBatch("a", "");
            TaskRow newest = list.StartBatch("b", "");

            Assert.Same(newest, list.Last);
        }

        [Fact]
        public void TaskRow_RaisesPropertyChangedForMutableColumns()
        {
            var row = new TaskRow();
            var changed = new System.Collections.Generic.List<string?>();
            row.PropertyChanged += (_, e) => changed.Add(e.PropertyName);

            row.Status = "完成";
            row.Info = "done";
            row.Cost = "3";
            row.DoneAt = "12:00";

            Assert.Equal(new[] { "Status", "Info", "Cost", "DoneAt" }, changed);
        }
    }
}
