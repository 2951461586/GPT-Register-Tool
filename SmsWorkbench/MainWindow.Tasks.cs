using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Globalization;
using System.Windows.Data;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using FluentWindow = Wpf.Ui.Controls.FluentWindow;

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Backend process, task list, deletion and cancellation actions
        private void RerunFailed_Click(object sender, RoutedEventArgs e)
        {
            var failedRows = allRows.Where(r =>
                (r.Status.Contains("失败") || r.Status.Contains("待处理") || r.Status.Contains("缺"))
                && (r.AccountType.Contains("Chatai") || r.AccountType.Contains("邮箱池"))
                && !string.IsNullOrWhiteSpace(r.RawLine)).ToList();

            if (failedRows.Count == 0)
            {
                MessageBox.Show("没有找到需要重注册的失败账号。", "提示", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            if (MessageBox.Show($"找到 {failedRows.Count} 条失败/待处理账号，确定重新注册？\n\n流程：注册→获取access token→生成支付链接→存session入库",
                "确认重注册", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return;

            string tempFile = Path.Combine(Path.GetTempPath(), "rerun_failed_" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            var lines = new List<string>();
            foreach (PoolRow row in failedRows)
            {
                string line = row.RawLine.Trim();
                if (line.Length > 0) lines.Add(line);
            }
            File.WriteAllLines(tempFile, lines, new UTF8Encoding(false));

            var args = new List<string> { "--chatai-mailbox-file", tempFile, "--count", lines.Count.ToString(), "--workers", "4" };
            AddProxy(args);
            AddPaypalOption(args);
            RunBackend("重新注册失败账号 (" + lines.Count + ")", args);
        }

        private void RebuildSqlite_Click(object sender, RoutedEventArgs e)
        {
            var args = new List<string> { "--rebuild-sqlite" };
            RunBackend("重建SQLite索引", args);
        }

        private void AccountGrid_SelectionChanged(object sender, SelectionChangedEventArgs e)
        {
            foreach (object item in e.AddedItems)
            {
                if (item is PoolRow row) row.IsChecked = true;
            }
        }

        private void AccountDetail_Click(object sender, RoutedEventArgs e)
        {
            if (sender is FrameworkElement element && element.DataContext is PoolRow row)
            {
                ShowAccountDetail(row);
            }
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
            ScrollTaskGridToBottom();
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

            var backendOutput = new StringBuilder();
            object backendOutputLock = new object();
            void CaptureBackendLine(string line)
            {
                lock (backendOutputLock)
                {
                    backendOutput.AppendLine(line);
                }
            }

            var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
            runningProcess = process;
            runningProcess.OutputDataReceived += (_, ev) =>
            {
                if (ev.Data == null) return;
                CaptureBackendLine(ev.Data);
                UiLog(ev.Data);
            };
            runningProcess.ErrorDataReceived += (_, ev) =>
            {
                if (ev.Data == null) return;
                CaptureBackendLine(ev.Data);
                UiLog(ev.Data);
            };
            runningProcess.Exited += (_, __) =>
            {
                Dispatcher.BeginInvoke(new Action(() =>
                {
                    task.Status = process.ExitCode == 0 ? "完成" : "失败";
                    task.Cost = ((int)(DateTime.Now - started).TotalSeconds).ToString();
                    task.DoneAt = SafeTime(DateTime.Now);
                    StatusText = taskName + " 已结束";
                    RefreshPools();
                    ScrollTaskGridToBottom();
                    if (taskName.StartsWith("一键扫号", StringComparison.OrdinalIgnoreCase))
                    {
                        string output;
                        lock (backendOutputLock)
                        {
                            output = backendOutput.ToString();
                        }
                        ShowAccountScanResultDialog(output);
                    }
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

        private string RunBackendWithResult(string taskName, List<string> args)
        {
            string script = Path.Combine(rootDir, "chatgpt_phone_reg.py");
            if (!File.Exists(script))
                throw new FileNotFoundException("找不到后端脚本：" + script);

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
                StandardErrorEncoding = Encoding.UTF8,
            };

            var output = new StringBuilder();
            var error = new StringBuilder();

            using (var process = new Process { StartInfo = psi })
            {
                process.OutputDataReceived += (_, ev) => { if (ev.Data != null) { lock (output) output.AppendLine(ev.Data); } };
                process.ErrorDataReceived += (_, ev) => { if (ev.Data != null) { lock (error) error.AppendLine(ev.Data); } };

                Log("启动：" + psi.FileName + " " + psi.Arguments);
                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                process.WaitForExit(120000); // 2 分钟超时

                if (!process.HasExited)
                {
                    try { process.Kill(); } catch { }
                    throw new TimeoutException("后端执行超时 (120s)");
                }

                string stdout;
                string stderr;
                lock (output) stdout = output.ToString().Trim();
                lock (error) stderr = error.ToString().Trim();

                // 从 stdout 中提取最后一个 JSON 块
                if (!string.IsNullOrEmpty(stdout))
                {
                    // 尝试找到最后一个 { 开始的 JSON
                    int lastBrace = stdout.LastIndexOf('{');
                    if (lastBrace >= 0)
                    {
                        string jsonPart = stdout.Substring(lastBrace);
                        if (jsonPart.Contains("}"))
                            return jsonPart;
                    }
                    return stdout;
                }

                if (!string.IsNullOrEmpty(stderr))
                    throw new Exception(stderr);

                return stdout;
            }
        }

        private void TaskGrid_Loaded(object sender, RoutedEventArgs e) => ScrollTaskGridToBottom();

        private void ScrollTaskGridToBottom()
        {
            if (TaskGrid == null || Tasks.Count == 0) return;
            Dispatcher.BeginInvoke(new Action(() =>
            {
                object last = Tasks[Tasks.Count - 1];
                TaskGrid.SelectedItem = last;
                TaskGrid.ScrollIntoView(last);
            }), DispatcherPriority.Background);
        }

        private void DeleteSelected_Click(object sender, RoutedEventArgs e)
        {
            var selected = allRows.Where(r => r.IsChecked).ToList();
            if (selected.Count == 0 && SelectedRow != null) selected.Add(SelectedRow);
            if (selected.Count == 0)
            {
                ShowThemeNoticeDialog("未选择记录", "请先勾选或选择要删除的记录。");
                return;
            }
            if (!ShowDeleteConfirmDialog(selected.Count)) return;
            foreach (PoolRow row in selected) DeleteRow(row);
            RefreshPools();
        }

        private void ShowThemeNoticeDialog(string title, string message)
        {
            var dialog = new Window
            {
                Title = title,
                Owner = this,
                Width = 420,
                Height = 190,
                MinWidth = 380,
                MinHeight = 170,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var body = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            body.Children.Add(new TextBlock
            {
                Text = title,
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 8)
            });
            body.Children.Add(new TextBlock
            {
                Text = message,
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)FindResource("TextSub")
            });
            root.Children.Add(body);

            var okButton = new Button
            {
                Content = "知道了",
                Width = 88,
                Style = (Style)FindResource("PrimaryButton"),
                HorizontalAlignment = HorizontalAlignment.Right
            };
            okButton.Click += (_, __) => dialog.Close();
            Grid.SetRow(okButton, 1);
            root.Children.Add(okButton);

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private bool ShowDeleteConfirmDialog(int count)
        {
            bool confirmed = false;
            var dialog = new Window
            {
                Title = "删除记录",
                Owner = this,
                Width = 460,
                Height = 230,
                MinWidth = 420,
                MinHeight = 210,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var body = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            body.Children.Add(new TextBlock
            {
                Text = "删除选中的 " + count + " 条记录？",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 8)
            });
            body.Children.Add(new TextBlock
            {
                Text = "将同步清理邮箱池、SQLite 索引和匹配的 session 文件。此操作不可撤销。",
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)FindResource("TextSub")
            });
            root.Children.Add(body);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right
            };
            var cancelButton = new Button { Content = "取消", Width = 76 };
            cancelButton.Click += (_, __) => dialog.Close();
            var deleteButton = new Button
            {
                Content = "删除",
                Width = 76,
                Style = (Style)FindResource("DangerButton")
            };
            deleteButton.Click += (_, __) =>
            {
                confirmed = true;
                dialog.Close();
            };
            actions.Children.Add(cancelButton);
            actions.Children.Add(deleteButton);
            Grid.SetRow(actions, 1);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
            return confirmed;
        }

        private void DeleteRow(PoolRow row)
        {
            try
            {
                string emailKey = NormalizeEmailKey(row.Identifier);
                int removedPoolLines = DeleteMailboxLines(row, emailKey);
                int removedSqliteRows = DeleteSqliteAccountRows(row, emailKey);
                int removedSessionFiles = DeleteSessionJsonFiles(row, emailKey);

                if (row.SourcePath.EndsWith(".json", StringComparison.OrdinalIgnoreCase)
                    && File.Exists(row.SourcePath)
                    && IsUnderDirectory(row.SourcePath, GetSessionsDir()))
                {
                    File.Delete(row.SourcePath);
                    removedSessionFiles++;
                }

                Log("删除账号：" + row.Identifier
                    + "，邮箱池 " + removedPoolLines
                    + " 条，SQLite " + removedSqliteRows
                    + " 条，session " + removedSessionFiles + " 个");
            }
            catch (Exception ex)
            {
                Log("删除失败：" + row.Identifier + " " + ex.Message);
            }
        }

        private int DeleteMailboxLines(PoolRow row, string emailKey)
        {
            int removed = 0;
            var paths = new List<string> { row.SourcePath, GetChataiMailboxFilePath(), GetMailboxTokenFile() };
            foreach (string path in paths.Where(p => !string.IsNullOrWhiteSpace(p)).Distinct(StringComparer.OrdinalIgnoreCase))
            {
                if (!File.Exists(path) || !path.EndsWith(".txt", StringComparison.OrdinalIgnoreCase)) continue;
                string rawLine = (row.RawLine ?? "").Trim();
                var lines = File.ReadAllLines(path, Encoding.UTF8).ToList();
                int before = lines.Count;
                lines.RemoveAll(line =>
                {
                    string value = line.Trim().TrimStart('\ufeff');
                    if (rawLine.Length > 0 && value.Equals(rawLine, StringComparison.OrdinalIgnoreCase)) return true;
                    string lineEmail = MailboxEmailForLine(value);
                    return emailKey.Length > 0 && NormalizeEmailKey(lineEmail) == emailKey;
                });
                int delta = before - lines.Count;
                if (delta <= 0) continue;
                File.WriteAllLines(path, lines, new UTF8Encoding(false));
                removed += delta;
            }
            return removed;
        }

        private int DeleteSqliteAccountRows(PoolRow row, string emailKey)
        {
            string dbPath = row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase)
                ? row.SourcePath
                : GetDatabasePath();
            if (!File.Exists(dbPath)) return 0;

            var rows = SqliteNative.Query(dbPath, "SELECT id,email,json_path FROM accounts");
            var deleteIds = new List<string>();
            string explicitId = row.SourcePath.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase) ? OnlyDigits(row.RawLine) : "";
            foreach (Dictionary<string, string> data in rows)
            {
                string id = data.TryGetValue("id", out string rawId) ? rawId : "";
                string email = data.TryGetValue("email", out string rawEmail) ? rawEmail : "";
                bool matches = explicitId.Length > 0 && id == explicitId;
                matches = matches || (emailKey.Length > 0 && NormalizeEmailKey(email) == emailKey);
                if (!matches) continue;
                deleteIds.Add(id);

                string jsonPath = data.TryGetValue("json_path", out string rawJsonPath) ? rawJsonPath : "";
                if (File.Exists(jsonPath) && IsUnderDirectory(jsonPath, GetSessionsDir()))
                {
                    TryDeleteFile(jsonPath);
                }
            }

            foreach (string id in deleteIds.Distinct())
            {
                SqliteNative.Execute(dbPath, "DELETE FROM accounts WHERE id=" + OnlyDigits(id));
            }
            return deleteIds.Distinct().Count();
        }

        private int DeleteSessionJsonFiles(PoolRow row, string emailKey)
        {
            int removed = 0;
            var dirs = new List<string> { GetSessionsDir(), rootDir };
            foreach (string dir in dirs.Where(Directory.Exists).Distinct(StringComparer.OrdinalIgnoreCase))
            {
                foreach (string path in Directory.GetFiles(dir, "session_*.json", SearchOption.TopDirectoryOnly))
                {
                    if (!SessionJsonMatchesEmail(path, emailKey)) continue;
                    if (TryDeleteFile(path)) removed++;
                }
            }
            string notes = (row.Notes ?? "").Trim();
            if (File.Exists(notes) && notes.EndsWith(".json", StringComparison.OrdinalIgnoreCase)
                && IsUnderDirectory(notes, GetSessionsDir()) && TryDeleteFile(notes))
            {
                removed++;
            }
            return removed;
        }

        private bool SessionJsonMatchesEmail(string path, string emailKey)
        {
            if (emailKey.Length == 0) return false;
            try
            {
                Dictionary<string, object> data = ReadJsonObject(path);
                return NormalizeEmailKey(GetString(data, "email")) == emailKey;
            }
            catch
            {
                return false;
            }
        }

        private string MailboxEmailForLine(string line)
        {
            string value = (line ?? "").Trim().TrimStart('\ufeff');
            if (value.Contains("----")) return value.Split(new[] { "----" }, StringSplitOptions.None).FirstOrDefault() ?? "";
            if (value.Contains("---")) return value.Split(new[] { "---" }, StringSplitOptions.None).FirstOrDefault() ?? "";
            return "";
        }

        private bool TryDeleteFile(string path)
        {
            try
            {
                if (!File.Exists(path)) return false;
                File.Delete(path);
                return true;
            }
            catch (Exception ex)
            {
                Log("删除文件失败：" + path + " " + ex.Message);
                return false;
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

        private void Settings_Click(object sender, RoutedEventArgs e) => ShowConfigDialog();
    }
}
