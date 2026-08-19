namespace SmsWorkbench
{
    public partial class MainWindow
    {
        private void RunUiTask(Func<Task> operation)
            => _ = RunUiTaskAsync(operation);

        private async Task RunUiTaskAsync(Func<Task> operation)
        {
            try
            {
                await operation();
            }
            catch (OperationCanceledException)
            {
            }
            catch (Exception ex)
            {
                Log("界面异步操作失败：" + SensitiveDataSanitizer.Redact(ex.Message));
                NotifyWarning("操作未完成，请查看运行日志。");
            }
        }

        // Path/config helpers, status formatting, external open/copy/log helpers.
        //
        // CLI argument construction lives in BackendCommandPlanner (window-independent,
        // unit-testable). Backend JSON business interpretation lives in
        // BackendResultInterpreter (window-independent, unit-testable).
        // Generic JSON plumbing lives in BackendJson.
        //
        // This file retains only the WPF-aware glue: settings resolution, proxy-pool
        // assembly, and thin delegates. The partials in Register.cs, Tasks.cs,
        // Payment.cs, and Export.cs have been migrated to call
        // BackendCommandPlanner.CreateXxx(...) and BackendResultInterpreter.xxx(...)
        // instead of building CLI arguments inline.
        private void AddRegistrationProxy(List<string> args)
        {
            List<string> pool = GetRegistrationProxyPool();
            AddConfiguredProxy(args, pool.FirstOrDefault() ?? GetRegistrationProxy());
            if (pool.Count > 1)
            {
                args.Add("--proxy-pool");
                args.Add(string.Join(Environment.NewLine, pool));
            }
        }

        private void AddMailboxProxy(List<string> args)
        {
            AddConfiguredProxy(args, GetMailboxProxy());
        }

        private static void AddConfiguredProxy(List<string> args, string proxy)
        {
            if (string.IsNullOrWhiteSpace(proxy)) return;
            args.Add("--proxy");
            args.Add(proxy.Trim());
        }

        private string GetRegistrationProxy()
        {
            string configured = FirstNonEmpty(
                settingsService.GetString("proxy.registration"),
                settingsService.GetString("registration_proxy"),
                settingsService.GetString("proxy.default"));
            return configured.Length > 0 ? configured : LocalNonPaymentProxy;
        }

        private List<string> GetRegistrationProxyPool()
        {
            var values = new List<string>();
            string primary = GetRegistrationProxy();
            if (primary.Length > 0) values.Add(primary);
            values.AddRange(settingsService.GetStringList("proxy.pool"));
            return values
                .Select(item => item.Trim())
                .Where(item => item.Length > 0)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToList();
        }

        private string GetMailboxProxy()
        {
            string configured = FirstNonEmpty(
                settingsService.GetString("mailbox_proxy"),
                settingsService.GetString("email_registration.mailbox_proxy"),
                settingsService.GetString("proxy.mailbox"));
            return configured.Length > 0 ? configured : LocalNonPaymentProxy;
        }

        // Account liveness (测活) must not inherit the rotating/paid registration
        // egress. A proxy-side 402 / quota failure there is an inconclusive probe,
        // not a dead account, so liveness routes through a fixed local egress
        // (default http://127.0.0.1:7897) unless an explicit proxy.liveness is set.
        private string GetLivenessProxy()
        {
            string configured = FirstNonEmpty(
                settingsService.GetString("proxy.liveness"),
                settingsService.GetString("liveness_proxy"));
            return configured.Length > 0 ? configured : LocalNonPaymentProxy;
        }

        private List<string> GetLivenessProxyPool()
        {
            string primary = GetLivenessProxy().Trim();
            return primary.Length > 0
                ? new List<string> { primary }
                : new List<string>();
        }

        private string GetConfiguredCfWorkerDomain()
        {
            string domain = settingsService.GetString("email_registration.cfworker_domain").Trim().TrimStart('@');
            if (domain.Length > 0) return domain;
            domain = settingsService.GetString("email_registration.cfworker.domain").Trim().TrimStart('@');
            return domain.Length > 0 ? domain : "liziai.cloud";
        }

        private string GetConfiguredSmailrDomain()
        {
            string domain = settingsService.GetString("email_registration.smailr.default_domain").Trim().TrimStart('@');
            if (domain.Length > 0) return domain;
            domain = settingsService.GetString("email_registration.smailr.domain").Trim().TrimStart('@');
            return domain.Length > 0 ? domain : "smailr.com";
        }

        private string NormalizePaymentMethod(string paymentMethod)
            => PaymentMethods.Normalize(paymentMethod);

        private void AddPaymentMethodItems(ComboBox box)
        {
            foreach (PaymentMethodOption method in PaymentMethods.RegistrationOptions)
                box.Items.Add(new ComboBoxItem { Content = method.DisplayName, Tag = method.Id });
        }

        private int CountValue()
        {
            return int.TryParse(CountText, out int value) && value > 0 ? value : 1;
        }

        private int PageSizeValue()
        {
            return int.TryParse(PageSizeText, out int value) && value > 0 ? Math.Min(value, 500) : 25;
        }

        private string GetSessionsDir()
        {
            return Path.Combine(rootDir, "sessions");
        }

        private string GetDatabasePath()
        {
            string configured = settingsService.GetString("storage.sqlite_path");
            if (configured.Length == 0) return Path.Combine(rootDir, "runtime", "accounts.sqlite3");
            string expanded = Environment.ExpandEnvironmentVariables(configured);
            return Path.IsPathRooted(expanded) ? expanded : Path.Combine(rootDir, expanded);
        }

        private string GetMailboxTokenFile()
        {
            string configured = settingsService.GetString("email_registration.token_file");
            string expanded = configured.Length > 0 ? Environment.ExpandEnvironmentVariables(configured) : "mailbox_tokens.txt";
            return Path.IsPathRooted(expanded) ? expanded : Path.Combine(rootDir, expanded);
        }

        // ── Thin delegates to the shared JSON plumbing (BackendJson) ─────
        // Kept so the control-event partials stay readable without qualifying
        // every lookup; all behavior is owned by BackendJson.

        private string GetString(Dictionary<string, object> data, string key)
            => BackendJson.GetString(data, key);

        private bool TryGetMap(Dictionary<string, object> data, string key, out Dictionary<string, object> map)
            => BackendJson.TryGetMap(data, key, out map);

        private string NestedString(Dictionary<string, object> data, params string[] path)
            => BackendJson.NestedString(data, path);

        private Dictionary<string, object> JsonTextToObject(string json)
            => BackendJson.TextToObject(json);

        private Dictionary<string, object> JsonDocumentToObject(JsonDocument document)
            => BackendJson.DocumentToObject(document);

        private object JsonValueToObject(JsonElement element)
            => BackendJson.ValueToObject(element);

        private Dictionary<string, object> JsonElementToDictionary(JsonElement element)
            => BackendJson.ElementToDictionary(element);

        private string FirstNonEmpty(params string[] values)
            => BackendJson.FirstNonEmpty(values);

        private static bool ParseBoolean(string value)
            => BackendJson.ParseBoolean(value);

        private string DbTimingText(Dictionary<string, string> data)
        {
            string pipeline = data.TryGetValue("pipeline_total_seconds", out string pipelineSeconds) ? pipelineSeconds : "";
            if (!string.IsNullOrWhiteSpace(pipeline) && pipeline != "0.0" && pipeline != "0") return pipeline + "s";
            string timing = data.TryGetValue("timing_total_seconds", out string timingSeconds) ? timingSeconds : "";
            return string.IsNullOrWhiteSpace(timing) || timing == "0.0" || timing == "0" ? "" : timing + "s";
        }

        private string UnixTimeText(string raw)
        {
            if (!long.TryParse(raw, out long seconds) || seconds <= 0) return "";
            return DateTimeOffset.FromUnixTimeSeconds(seconds).LocalDateTime.ToString("yyyy-MM-dd HH:mm:ss");
        }

        private string OnlyDigits(string raw)
        {
            string digits = new string((raw ?? "").Where(char.IsDigit).ToArray());
            return digits.Length == 0 ? "0" : digits;
        }

        private string DisplayText(object value)
        {
            if (value is ComboBoxItem item) return Convert.ToString(item.Content) ?? "";
            return Convert.ToString(value) ?? "";
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
                    if (File.Exists(path) && ShouldOpenWithNotepad(path))
                    {
                        OpenWithNotepad(path);
                        return;
                    }
                    Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
                    return;
                }
                if (Path.GetExtension(path).Length > 0)
                {
                    string directory = Path.GetDirectoryName(Path.GetFullPath(path)) ?? rootDir;
                    Directory.CreateDirectory(directory);
                    string example = Path.Combine(rootDir, "config.example.json");
                    if (Path.GetFileName(path).Equals("config.json", StringComparison.OrdinalIgnoreCase) && File.Exists(example))
                    {
                        File.Copy(example, path);
                    }
                    else if (!File.Exists(path))
                    {
                        File.WriteAllText(path, "", Encoding.UTF8);
                    }
                    OpenWithNotepad(path);
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

        private bool ShouldOpenWithNotepad(string path)
        {
            string extension = Path.GetExtension(path).ToLowerInvariant();
            return extension == ".json" || extension == ".txt" || extension == ".log";
        }

        private void OpenWithNotepad(string path)
        {
            var psi = new ProcessStartInfo("notepad.exe")
            {
                UseShellExecute = false
            };
            psi.ArgumentList.Add(path);
            Process.Start(psi);
        }

        private void OpenUrl(string url)
        {
            try
            {
                if (!Uri.TryCreate(url, UriKind.Absolute, out Uri uri) ||
                    (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
                {
                    Log("无效链接：" + url);
                    return;
                }
                Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                Log("打开链接失败：" + ex.Message);
            }
        }

        private void OpenPayPalUrl(string url, string accountEmail = "")
            => RunUiTask(() => OpenPayPalUrlAsync(url, accountEmail));

        private async Task OpenPayPalUrlAsync(string url, string accountEmail = "")
        {
            url = await ResolveBackendPaymentUrlAsync(url, accountEmail);
            if (!IsHttpUrl(url))
            {
                Log("无效支付链接：" + url);
                return;
            }
            string chrome = FindChromePath();
            if (chrome.Length == 0)
            {
                Log("未找到 Chrome，使用系统默认浏览器打开支付链接。");
                OpenUrl(url);
                return;
            }
            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = chrome,
                    UseShellExecute = false
                };
                psi.ArgumentList.Add("--new-window");
                psi.ArgumentList.Add("--incognito");
                psi.ArgumentList.Add(url);
                Process.Start(psi);
                Log("已用 Chrome 无痕窗口打开支付链接。");
            }
            catch (Exception ex)
            {
                Log("Chrome 打开失败：" + ex.Message);
                OpenUrl(url);
            }
        }

        private void CopyPayPalUrl(string url, string accountEmail = "")
            => RunUiTask(() => CopyPayPalUrlAsync(url, accountEmail));

        private async Task CopyPayPalUrlAsync(string url, string accountEmail = "")
        {
            url = await ResolveBackendPaymentUrlAsync(url, accountEmail);
            if (!IsHttpUrl(url))
            {
                Log("无效支付链接，无法复制。");
                return;
            }
            try
            {
                Clipboard.SetText(url);
                Log("支付链接已复制。");
            }
            catch (Exception ex)
            {
                Log("复制支付链接失败：" + ex.Message);
            }
        }

        private async Task<string> ResolveBackendPaymentUrlAsync(string url, string accountEmail)
        {
            if (!string.Equals(url, "backend://payment-url", StringComparison.OrdinalIgnoreCase)) return url;
            try
            {
                return (await desktopRead.ReadPaymentUrlAsync("", accountEmail)).Trim();
            }
            catch (Exception ex)
            {
                Log("读取支付链接 backend 失败：" + SensitiveDataSanitizer.Redact(ex.Message));
                return "";
            }
        }

        private string FindChromePath()
        {
            string[] candidates =
            {
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Google", "Chrome", "Application", "chrome.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), "Google", "Chrome", "Application", "chrome.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Google", "Chrome", "Application", "chrome.exe")
            };
            return candidates.FirstOrDefault(File.Exists) ?? "";
        }

        private bool IsHttpUrl(string url)
        {
            return Uri.TryCreate(url, UriKind.Absolute, out Uri uri)
                && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps);
        }

        private void ClearLog_Click(object sender, RoutedEventArgs e)
        {
            LogText = "";
        }

        private void Log(string text)
        {
            LogPresanitized(SensitiveDataSanitizer.Redact(text));
        }

        /// <summary>
        /// Appends an already-redacted line. Backend lines are redacted once in
        /// the pump; re-redacting here (the old behaviour) doubled regex cost on
        /// every output line.
        /// </summary>
        private void LogPresanitized(string safeText, bool debug = false)
        {
            string line = "[" + DateTime.Now.ToString("HH:mm:ss") + "] " + safeText + Environment.NewLine;
            if (debug)
                logger?.Debug("[backend] {Line}", safeText);
            else
                logger?.Information("{Message}", safeText);
            if (LogTextBox != null && LogTextBox.IsLoaded)
            {
                // Append into the control directly: whole-string reassignment of
                // LogText re-rendered the entire buffer on every line (O(n²)).
                LogTextBox.AppendText(line);
                logText += line; // keep the bound property consistent without re-render
                return;
            }
            LogText += line;
        }

        private void UiLog(string text)
        {
            // Called from Progress<T> callbacks, which already post to the UI
            // SyncContext; the extra Dispatcher.BeginInvoke was a second hop.
            LogPresanitized(SensitiveDataSanitizer.Redact(text), debug: true);
        }

        private void NotifySuccess(string message)
        {
            snackbarService.Show("完成", message, Wpf.Ui.Controls.ControlAppearance.Success, null, TimeSpan.FromSeconds(4));
        }

        private void NotifyWarning(string message)
        {
            snackbarService.Show("注意", message, Wpf.Ui.Controls.ControlAppearance.Caution, null, TimeSpan.FromSeconds(5));
        }

        private void NotifyInfo(string message)
        {
            snackbarService.Show("提示", message, Wpf.Ui.Controls.ControlAppearance.Info, null, TimeSpan.FromSeconds(4));
        }

        private void OnPropertyChanged(string name)
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
        }
    }
}
