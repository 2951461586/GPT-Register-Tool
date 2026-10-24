// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// The batch task list shown in the main window: the rows plus the running
    /// "批次 N" counter. Window-independent, so naming and ordering rules are
    /// xunit-testable (placement rule 8); scrolling the grid stays in the view.
    /// </summary>
    public sealed class TaskListViewModel : ObservableObject
    {
        private int nextSequence = 1;

        public ObservableCollection<TaskRow> Tasks { get; } = new ObservableCollection<TaskRow>();

        public TaskRow? Last => Tasks.Count == 0 ? null : Tasks[Tasks.Count - 1];

        /// <summary>Rows created before the switch keep their text; new rows use the new language.</summary>
        public void RefreshLanguage()
        {
            OnPropertyChanged(string.Empty);
        }

        /// <summary>Appends a running row named "批次 N" and returns it for later updates.</summary>
        public TaskRow StartBatch(string taskName, string info)
        {
            var row = new TaskRow
            {
                Name = Loc.F("批次 {0}", nextSequence++),
                Task = taskName,
                Status = Loc.T("运行中"),
                Info = info,
            };
            Tasks.Add(row);
            return row;
        }
    }

    public sealed partial class TaskRow : ObservableObject
    {
        [ObservableProperty] private string status = "";
        [ObservableProperty] private string cost = "";
        [ObservableProperty] private string doneAt = "";
        // Info IS mutated while the task runs (MainWindow.Tasks.cs) and is bound
        // by MainWindow.xaml, so unlike Name/Task/Retry below it must raise
        // PropertyChanged — an auto-property here silently froze the column.
        [ObservableProperty] private string info = "";
        // Name/Task/Retry are assigned once before the row enters the
        // ObservableCollection and never mutated afterwards, so they need no
        // notification. Do not "fix" them without checking for runtime writers.
        public string Name { get; set; } = "";
        public string Task { get; set; } = "";
        public string Retry { get; set; } = "0";
    }
}
