using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Threading;

namespace SmsWorkbench
{
    public partial class MainWindow : Window, INotifyPropertyChanged
    {
        private readonly string rootDir;
        private readonly ObservableCollection<PoolRow> allRows = new ObservableCollection<PoolRow>();
        private readonly ICollectionView filteredRows;
        private Process runningProcess;
        private int taskSeq = 1;
        private string searchText = "";
        private string countText = "5";
        private string proxyText = "";
        private object scopeFilter = "全部";
        private object smsProvider = "smsbower";
        private string logText = "";
        private string statusText = "就绪";

        public event PropertyChangedEventHandler PropertyChanged;

        public ObservableCollection<TaskRow> Tasks { get; } = new ObservableCollection<TaskRow>();

        public ICollectionView FilteredRows => filteredRows;

        public PoolRow SelectedRow { get; set; }

        public int SelectedTabIndex { get; set; }

        public string SearchText
        {
            get => searchText;
            set { searchText = value ?? ""; OnPropertyChanged(nameof(SearchText)); filteredRows.Refresh(); }
        }

        public string CountText
        {
            get => countText;
            set { countText = value ?? "1"; OnPropertyChanged(nameof(CountText)); }
        }

        public string ProxyText
        {
            get => proxyText;
            set { proxyText = value ?? ""; OnPropertyChanged(nameof(ProxyText)); }
        }

        public object ScopeFilter
        {
            get => scopeFilter;
            set { scopeFilter = value; OnPropertyChanged(nameof(ScopeFilter)); filteredRows.Refresh(); }
        }

        public object SmsProvider
        {
            get => smsProvider;
            set { smsProvider = value; OnPropertyChanged(nameof(SmsProvider)); }
        }

        public string LogText
        {
            get => logText;
            set { logText = value ?? ""; OnPropertyChanged(nameof(LogText)); }
        }

        public string StatusText
        {
            get => statusText;
            set { statusText = value ?? ""; OnPropertyChanged(nameof(StatusText)); }
        }

        public MainWindow()
        {
            InitializeComponent();
            DataContext = this;

            rootDir = Directory.GetParent(AppDomain.CurrentDomain.BaseDirectory)?.FullName ?? AppDomain.CurrentDomain.BaseDirectory;
            if (Path.GetFileName(rootDir).Equals("net10", StringComparison.OrdinalIgnoreCase))
            {
                rootDir = Directory.GetParent(Directory.GetParent(rootDir)?.FullName ?? rootDir)?.FullName ?? rootDir;
            }
            if (Path.GetFileName(rootDir).Equals("dist", StringComparison.OrdinalIgnoreCase))
            {
                rootDir = Directory.GetParent(rootDir)?.FullName ?? rootDir;
            }

            filteredRows = CollectionViewSource.GetDefaultView(allRows);
            filteredRows.Filter = FilterRow;
            ScopeFilter = "全部";
            SmsProvider = "smsbower";
            RefreshPools();
        }

        private bool FilterRow(object item)
        {
            var row = item as PoolRow;
            if (row == null) return false;
            string scope = DisplayText(ScopeFilter);
            string term = (SearchText ?? "").Trim().ToLowerInvariant();

            if (scope == "邮箱池" && !row.AccountType.Contains("邮箱")) return false;
            if (scope == "号池" && !row.AccountType.Contains("Free")) return false;
            if (scope == "失败/待处理" && !row.Status.Contains("待") && !row.Status.Contains("缺") && !row.Status.Contains("失败")) return false;
            if (term.Length == 0) return true;

            string text = (row.Identifier + " " + row.AccountType + " " + row.Status + " " + row.Notes).ToLowerInvariant();
            return text.Contains(term);
        }

        private void RefreshPools()
        {
            allRows.Clear();
            LoadOutlookPool();
            LoadSessionPool();
            filteredRows.Refresh();
            int mailbox = allRows.Count(r => r.AccountType.Contains("邮箱"));
            int free = allRows.Count(r => r.AccountType.Contains("Free"));
            StatusText = $"共 {allRows.Count} 条；邮箱池 {mailbox}；号池 {free}";
            Log("池状态已刷新。");
        }

        private void LoadOutlookPool()
        {
            string resultsDir = GetOutlookResultsDir();
            LoadOutlookTokenFile(Path.Combine(resultsDir, "outlook_token.txt"));
            LoadUnloggedFile(Path.Combine(resultsDir, "unlogged_email.txt"));

            string nbToken = Path.GetFullPath(Path.Combine(rootDir, "..", "nb-register", "outlook-register-service", "Results", "outlook_token.txt"));
            if (!nbToken.StartsWith(resultsDir, StringComparison.OrdinalIgnoreCase))
            {
                LoadOutlookTokenFile(nbToken);
            }
        }

        private void LoadOutlookTokenFile(string path)
        {
            if (!File.Exists(path)) return;
            string[] lines = File.ReadAllLines(path, Encoding.UTF8);
            for (int i = 0; i < lines.Length; i++)
            {
                string line = lines[i].Trim();
                if (line.Length == 0 || line.StartsWith("#")) continue;
                string[] parts = line.Split(new[] { "---" }, StringSplitOptions.None);
                if (parts.Length < 3) continue;
                allRows.Add(new PoolRow
                {
                    Id = "M" + (i + 1),
                    CreatedAt = SafeTime(File.GetLastWriteTime(path)),
                    CompletedAt = SafeTime(File.GetLastWriteTime(path)),
                    Identifier = parts[0].Trim(),
                    AccountType = "邮箱-已授权",
                    Status = "已授权",
                    RefreshToken = Mask(parts[2]),
                    Notes = path,
                    SourcePath = path,
                    RawLine = line
                });
            }
        }

        private void LoadUnloggedFile(string path)
        {
            if (!File.Exists(path)) return;
            string[] lines = File.ReadAllLines(path, Encoding.UTF8);
            for (int i = 0; i < lines.Length; i++)
            {
                string line = lines[i].Trim();
                if (line.Length == 0 || line.StartsWith("#")) continue;
                int idx = line.IndexOf(':');
                if (idx <= 0) continue;
                string email = line.Substring(0, idx).Trim();
                if (allRows.Any(r => r.Identifier.Equals(email, StringComparison.OrdinalIgnoreCase))) continue;
                allRows.Add(new PoolRow
                {
                    Id = "U" + (i + 1),
                    CreatedAt = SafeTime(File.GetLastWriteTime(path)),
                    Identifier = email,
                    AccountType = "邮箱-未授权",
                    Status = "待OAuth",
                    Notes = path,
                    SourcePath = path,
                    RawLine = line
                });
            }
        }

        private void LoadSessionPool()
        {
            foreach (string path in Directory.GetFiles(rootDir, "session_*.json", SearchOption.AllDirectories))
            {
                if (path.IndexOf("\\SmsWorkbench\\", StringComparison.OrdinalIgnoreCase) >= 0) continue;
                try
                {
                    Dictionary<string, object> data = ReadJsonObject(path);
                    string email = GetString(data, "email");
                    string phone = GetString(data, "phone");
                    string refresh = GetString(data, "refresh_token");
                    allRows.Add(new PoolRow
                    {
                        Id = "S" + (allRows.Count + 1),
                        CreatedAt = SafeTime(File.GetCreationTime(path)),
                        CompletedAt = SafeTime(File.GetLastWriteTime(path)),
                        Identifier = email.Length > 0 ? email : phone,
                        AccountType = "Free",
                        Status = refresh.Length > 0 ? "已保存" : "缺refresh",
                        RefreshToken = Mask(refresh),
                        Notes = path,
                        SourcePath = path
                    });
                }
                catch (Exception ex)
                {
                    Log("读取 session 失败：" + path + " " + ex.Message);
                }
            }
        }

        private void BatchOutlook_Click(object sender, RoutedEventArgs e)
        {
            var args = new List<string> { "--outlook-register", "--count", CountValue().ToString() };
            AddProxy(args);
            RunBackend("批量注册Outlook", args);
        }

        private void BatchFree_Click(object sender, RoutedEventArgs e)
        {
            var args = new List<string> { "--count", CountValue().ToString(), "--sms-provider", DisplayText(SmsProvider).Length > 0 ? DisplayText(SmsProvider) : "smsbower" };
            AddProxy(args);
            RunBackend("批量注册Free", args);
        }

        private void RunBackend(string taskName, List<string> args)
        {
            if (runningProcess != null && !runningProcess.HasExited)
            {
                MessageBox.Show("已有批次正在运行，请先取消或等待完成。", "运行中", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            string script = Path.Combine(rootDir, "chatgpt_phone_reg.py");
            if (!File.Exists(script))
            {
                MessageBox.Show("找不到后端脚本：" + script, "错误", MessageBoxButton.OK, MessageBoxImage.Error);
                return;
            }

            var task = new TaskRow { Name = "批次 " + taskSeq++, Task = taskName, Status = "运行中", Info = string.Join(" ", args) };
            Tasks.Add(task);
            DateTime started = DateTime.Now;

            var psi = new ProcessStartInfo
            {
                FileName = "python",
                Arguments = Quote(script) + " " + JoinArgs(args),
                WorkingDirectory = rootDir,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };

            runningProcess = new Process { StartInfo = psi, EnableRaisingEvents = true };
            runningProcess.OutputDataReceived += (_, ev) => { if (ev.Data != null) UiLog(ev.Data); };
            runningProcess.ErrorDataReceived += (_, ev) => { if (ev.Data != null) UiLog(ev.Data); };
            runningProcess.Exited += (_, __) =>
            {
                Dispatcher.BeginInvoke(new Action(() =>
                {
                    task.Status = runningProcess.ExitCode == 0 ? "完成" : "失败";
                    task.Cost = ((int)(DateTime.Now - started).TotalSeconds).ToString();
                    task.DoneAt = SafeTime(DateTime.Now);
                    StatusText = taskName + " 已结束";
                    RefreshPools();
                }), DispatcherPriority.Background);
            };

            try
            {
                Log("启动：" + psi.FileName + " " + psi.Arguments);
                runningProcess.Start();
                runningProcess.BeginOutputReadLine();
                runningProcess.BeginErrorReadLine();
                StatusText = taskName + " 运行中";
            }
            catch (Exception ex)
            {
                task.Status = "启动失败";
                Log("启动失败：" + ex.Message);
            }
        }

        private void DeleteSelected_Click(object sender, RoutedEventArgs e)
        {
            var selected = allRows.Where(r => r.IsChecked).ToList();
            if (selected.Count == 0 && SelectedRow != null) selected.Add(SelectedRow);
            if (selected.Count == 0)
            {
                MessageBox.Show("请先勾选或选择要删除的记录。", "提示", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            if (MessageBox.Show("确定删除选中的 " + selected.Count + " 条记录？", "确认", MessageBoxButton.YesNo, MessageBoxImage.Warning) != MessageBoxResult.Yes) return;
            foreach (PoolRow row in selected) DeleteRow(row);
            RefreshPools();
        }

        private void DeleteRow(PoolRow row)
        {
            try
            {
                if (row.SourcePath.EndsWith(".json", StringComparison.OrdinalIgnoreCase))
                {
                    File.Delete(row.SourcePath);
                    Log("删除文件：" + row.SourcePath);
                    return;
                }
                if (File.Exists(row.SourcePath) && !string.IsNullOrWhiteSpace(row.RawLine))
                {
                    var lines = File.ReadAllLines(row.SourcePath, Encoding.UTF8).ToList();
                    lines.RemoveAll(line => line.Trim() == row.RawLine.Trim());
                    File.WriteAllLines(row.SourcePath, lines, Encoding.UTF8);
                    Log("删除池记录：" + row.Identifier);
                }
            }
            catch (Exception ex)
            {
                Log("删除失败：" + row.Identifier + " " + ex.Message);
            }
        }

        private void CancelBatch_Click(object sender, RoutedEventArgs e)
        {
            if (runningProcess == null || runningProcess.HasExited)
            {
                Log("当前没有运行中的批次。");
                return;
            }
            try
            {
                runningProcess.Kill(true);
                Log("已取消当前批次。");
            }
            catch (Exception ex)
            {
                Log("取消失败：" + ex.Message);
            }
        }

        private void Refresh_Click(object sender, RoutedEventArgs e) => RefreshPools();

        private void Settings_Click(object sender, RoutedEventArgs e) => OpenPath(Path.Combine(rootDir, "config.json"));

        private void OpenResults_Click(object sender, RoutedEventArgs e) => OpenPath(GetOutlookResultsDir());

        private void ApplyFilter_Click(object sender, RoutedEventArgs e) => filteredRows.Refresh();

        private void ClearSelection_Click(object sender, RoutedEventArgs e)
        {
            foreach (PoolRow row in allRows) row.IsChecked = false;
        }

        private void ApplyProxy_Click(object sender, RoutedEventArgs e)
        {
            Log("代理出口已设置为本次运行参数：" + (ProxyText.Trim().Length == 0 ? "未设置" : ProxyText.Trim()));
        }

        private void ShowOAuth_Click(object sender, RoutedEventArgs e)
        {
            ScopeFilter = "邮箱池";
        }

        private void Stub_Click(object sender, RoutedEventArgs e)
        {
            Log("该功能当前未接入此项目后端。");
        }

        private void AddProxy(List<string> args)
        {
            if (!string.IsNullOrWhiteSpace(ProxyText))
            {
                args.Add("--proxy");
                args.Add(ProxyText.Trim());
            }
        }

        private int CountValue()
        {
            return int.TryParse(CountText, out int value) && value > 0 ? value : 1;
        }

        private string GetOutlookResultsDir()
        {
            string configured = ConfigString("outlook_register", "results_dir");
            return configured.Length > 0 ? Environment.ExpandEnvironmentVariables(configured) : Path.Combine(rootDir, "outlook_results");
        }

        private string ConfigString(string section, string key)
        {
            string path = Path.Combine(rootDir, "config.json");
            if (!File.Exists(path)) return "";
            try
            {
                Dictionary<string, object> data = ReadJsonObject(path);
                if (!data.TryGetValue(section, out object sectionObj)) return "";
                if (sectionObj is not Dictionary<string, object> sectionData) return "";
                return sectionData.TryGetValue(key, out object value) ? Convert.ToString(value) ?? "" : "";
            }
            catch
            {
                return "";
            }
        }

        private Dictionary<string, object> ReadJsonObject(string path)
        {
            var output = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            using JsonDocument document = JsonDocument.Parse(File.ReadAllText(path, Encoding.UTF8));
            if (document.RootElement.ValueKind != JsonValueKind.Object) return output;
            foreach (JsonProperty property in document.RootElement.EnumerateObject())
            {
                output[property.Name] = JsonValueToObject(property.Value);
            }
            return output;
        }

        private object JsonValueToObject(JsonElement element)
        {
            switch (element.ValueKind)
            {
                case JsonValueKind.String: return element.GetString() ?? "";
                case JsonValueKind.Number:
                    return element.TryGetInt64(out long n) ? n : element.GetDouble();
                case JsonValueKind.True: return true;
                case JsonValueKind.False: return false;
                case JsonValueKind.Object:
                    var obj = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                    foreach (JsonProperty property in element.EnumerateObject()) obj[property.Name] = JsonValueToObject(property.Value);
                    return obj;
                case JsonValueKind.Array:
                    return element.EnumerateArray().Select(JsonValueToObject).ToList();
                default: return "";
            }
        }

        private string GetString(Dictionary<string, object> data, string key)
        {
            return data.TryGetValue(key, out object value) && value != null ? Convert.ToString(value) ?? "" : "";
        }

        private string DisplayText(object value)
        {
            if (value is ComboBoxItem item) return Convert.ToString(item.Content) ?? "";
            return Convert.ToString(value) ?? "";
        }

        private string JoinArgs(List<string> args) => string.Join(" ", args.Select(Quote));

        private string Quote(string value)
        {
            value ??= "";
            return value.IndexOfAny(new[] { ' ', '\t', '"', '&', '|' }) < 0 ? value : "\"" + value.Replace("\"", "\\\"") + "\"";
        }

        private string Mask(string value)
        {
            value = (value ?? "").Trim();
            return value.Length <= 12 ? value : value.Substring(0, 6) + "..." + value.Substring(value.Length - 4);
        }

        private string SafeTime(DateTime time) => time.ToString("yyyy-MM-dd HH:mm:ss");

        private void OpenPath(string path)
        {
            try
            {
                if (File.Exists(path) || Directory.Exists(path))
                {
                    Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
                    return;
                }
                if (Path.GetExtension(path).Length > 0)
                {
                    string example = Path.Combine(rootDir, "config.example.json");
                    if (Path.GetFileName(path).Equals("config.json", StringComparison.OrdinalIgnoreCase) && File.Exists(example))
                    {
                        File.Copy(example, path);
                    }
                    Process.Start(new ProcessStartInfo("notepad.exe", path) { UseShellExecute = true });
                    return;
                }
                Directory.CreateDirectory(path);
                Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                Log("打开失败：" + ex.Message);
            }
        }

        private void Log(string text)
        {
            LogText += "[" + DateTime.Now.ToString("HH:mm:ss") + "] " + text + Environment.NewLine;
        }

        private void UiLog(string text)
        {
            Dispatcher.BeginInvoke(new Action(() => Log(text)), DispatcherPriority.Background);
        }

        private void OnPropertyChanged(string name)
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
        }
    }

    public sealed class PoolRow : INotifyPropertyChanged
    {
        private bool isChecked;
        public string Id { get; set; } = "";
        public string CreatedAt { get; set; } = "";
        public string CompletedAt { get; set; } = "";
        public string Identifier { get; set; } = "";
        public string AccountType { get; set; } = "";
        public string Status { get; set; } = "";
        public string RefreshToken { get; set; } = "";
        public string Proxy { get; set; } = "";
        public string Notes { get; set; } = "";
        public string SourcePath { get; set; } = "";
        public string RawLine { get; set; } = "";
        public bool IsChecked
        {
            get => isChecked;
            set { isChecked = value; PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(IsChecked))); }
        }
        public event PropertyChangedEventHandler PropertyChanged;
    }

    public sealed class TaskRow : INotifyPropertyChanged
    {
        private string status = "";
        private string cost = "";
        private string doneAt = "";
        public string Name { get; set; } = "";
        public string Task { get; set; } = "";
        public string Info { get; set; } = "";
        public string Retry { get; set; } = "0";
        public string Status { get => status; set { status = value; Notify(nameof(Status)); } }
        public string Cost { get => cost; set { cost = value; Notify(nameof(Cost)); } }
        public string DoneAt { get => doneAt; set { doneAt = value; Notify(nameof(DoneAt)); } }
        public event PropertyChangedEventHandler PropertyChanged;
        private void Notify(string name) => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
    }
}
