namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Account import/export, scan result and export JSON helpers.
        //
        // CLI argument construction is delegated to BackendCommandPlanner;
        // backend JSON business interpretation is delegated to
        // BackendResultInterpreter.

        private void ImportPaidCpa_Click(object sender, RoutedEventArgs e)
        {
            string target = ShowImportTargetDialog("一键导入");
            if (target.Length == 0) return;

            var selected = SelectedRowsOrCurrent()
                .Where(IsImportableAccountRow)
                .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                .Select(g => g.First())
                .ToList();
            var rows = selected.Count > 0
                ? selected
                : allRows.Where(IsImportableAccountRow)
                    .Where(r => !string.IsNullOrWhiteSpace(r.Identifier))
                    .GroupBy(r => r.Identifier.Trim().ToLowerInvariant())
                    .Select(g => g.First())
                    .ToList();

            if (rows.Count == 0)
            {
                MessageBox.Show("没有找到可导入账号。请先注册账号并获得 access_token/session。", "一键导入", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }

            var plan = BackendCommandPlanner.CreateAccountImport(
                target,
                rows.Select(r => r.Identifier.Trim()).ToList());
            RunBackend(plan.TaskName, plan.Arguments.ToList());
        }

        private void ExportAccounts_Click(object sender, RoutedEventArgs e)
        {
            string format = ShowExportFormatDialog();
            if (format.Length == 0) return;

            var rows = ExportCandidateRows();
            if (format.Equals("txt", StringComparison.OrdinalIgnoreCase))
            {
                ExportAccountsTxt(rows);
                return;
            }
            if (format.Equals("json", StringComparison.OrdinalIgnoreCase))
            {
                ExportAccountsJson(rows);
                return;
            }
            ExportAccountsConvertedJson(rows, format);
        }

        private List<PoolRow> ExportCandidateRows()
        {
            var rows = SelectedRowsOrCurrent();
            if (rows.Count == 0)
            {
                rows = allRows.Where(FilterRow).ToList();
            }
            if (rows.Count == 0)
            {
                rows = allRows.ToList();
            }
            return rows;
        }

        private void ExportAccountsTxt(List<PoolRow> rows)
        {
            var lines = new List<string>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            int skipped = 0;
            foreach (PoolRow row in rows)
            {
                if (TryBuildAccountExportLine(row, out string line))
                {
                    if (seen.Add(line))
                    {
                        lines.Add(line);
                    }
                }
                else
                {
                    skipped++;
                }
            }

            if (lines.Count == 0)
            {
                ShowThemedInfoDialog("一键导出", "没有找到可导出的账号记录。仅支持包含邮箱、密码、客户端ID、刷新令牌的邮箱记录；CFWorker 或缺少密码/刷新令牌的记录会被跳过。");
                return;
            }

            string outputDir = Path.Combine(rootDir, "runtime");
            Directory.CreateDirectory(outputDir);
            string outputPath = Path.Combine(outputDir, "account-" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".txt");
            File.WriteAllLines(outputPath, lines, new UTF8Encoding(false));
            Log("One-click export wrote " + lines.Count + " account(s), skipped " + skipped + ": " + outputPath);
            ShowExportCompleteDialog(outputPath, lines.Count, skipped, "TXT", "账号----密码----客户端ID----刷新令牌");
        }

        private void ExportAccountsJson(List<PoolRow> rows)
            => RunUiTask(() => ExportAccountsJsonAsync(rows));

        private async Task ExportAccountsJsonAsync(List<PoolRow> rows)
        {
            var collected = await CollectAccountExportJsonAsync(rows);
            if (collected.Items.Count == 0)
            {
                ShowThemedInfoDialog("一键导出", "没有找到可导出的 JSON 账号记录。需要账号已生成 session/auth_session 或 SQLite 原始记录。");
                return;
            }

            string outputDir = Path.Combine(rootDir, "runtime", "account_json");
            Directory.CreateDirectory(outputDir);
            string outputPath = Path.Combine(outputDir, "account-" + DateTime.Now.ToString("yyyyMMdd_HHmmss") + ".json");
            object payload = collected.Items.Count == 1 ? collected.Items[0] : collected.Items;
            var options = new JsonSerializerOptions { WriteIndented = true };
            await Task.Run(() => File.WriteAllText(outputPath, JsonSerializer.Serialize(payload, options), new UTF8Encoding(false)));
            Log("One-click JSON export wrote " + collected.Items.Count + " account(s), skipped " + collected.Skipped + ": " + outputPath);
            ShowExportCompleteDialog(outputPath, collected.Items.Count, collected.Skipped, "JSON", "原始账号 session JSON；保留 RT 字段，未获取 RT 的账号默认留空");
        }

        private sealed record CollectedAccountExport(List<Dictionary<string, object>> Items, int Skipped);

        private async Task<CollectedAccountExport> CollectAccountExportJsonAsync(List<PoolRow> rows)
        {
            var items = new List<Dictionary<string, object>>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            int skipped = 0;
            foreach (PoolRow row in rows)
            {
                Dictionary<string, object> item = await BuildAccountExportJsonAsync(row);
                if (item != null)
                {
                    string key = JsonExportDedupKey(item, row);
                    if (seen.Add(key))
                    {
                        items.Add(item);
                    }
                }
                else
                {
                    skipped++;
                }
            }
            return new CollectedAccountExport(items, skipped);
        }

        private void ExportAccountsConvertedJson(List<PoolRow> rows, string format)
            => RunUiTask(() => ExportAccountsConvertedJsonAsync(rows, format));

        private async Task ExportAccountsConvertedJsonAsync(List<PoolRow> rows, string format)
        {
            var collected = await CollectAccountExportJsonAsync(rows);
            if (collected.Items.Count == 0)
            {
                ShowThemedInfoDialog("一键导出", "没有找到可转换的账号 session。需要账号已生成 access_token/session/auth_session 或 SQLite 原始记录。");
                return;
            }
            List<Dictionary<string, object>> items = collected.Items;
            int skipped = collected.Skipped;

            string normalized = (format ?? "cpa").Trim().ToLowerInvariant();
            string outputDir = Path.Combine(rootDir, "runtime", "account_json");
            Directory.CreateDirectory(outputDir);
            string stamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
            string sourcePath = Path.Combine(Path.GetTempPath(), "account_export_source_" + stamp + ".json");
            string outputPath = Path.Combine(outputDir, "account-" + normalized + "-" + stamp + ".json");
            object payload = items.Count == 1 ? items[0] : items;
            var options = new JsonSerializerOptions { WriteIndented = true };
            await Task.Run(() => File.WriteAllText(sourcePath, JsonSerializer.Serialize(payload, options), new UTF8Encoding(false)));

            try
            {
                var plan = BackendCommandPlanner.CreateSessionConversion(sourcePath, normalized, outputPath);
                await RunBackendWithResultAsync(plan.TaskName, plan.Arguments.ToList());
            }
            catch (Exception ex)
            {
                Log("账号格式转换失败：" + ex.Message);
                ShowThemedInfoDialog("一键导出", "账号格式转换失败：" + ex.Message);
                return;
            }
            finally
            {
                try { if (File.Exists(sourcePath)) File.Delete(sourcePath); } catch { }
            }

            if (!File.Exists(outputPath) || new FileInfo(outputPath).Length == 0)
            {
                ShowThemedInfoDialog("一键导出", "账号格式转换没有生成输出文件，请查看下方日志确认 converter 结果。");
                return;
            }

            Log("One-click converted export wrote " + items.Count + " account(s), skipped " + skipped + ", format=" + normalized + ": " + outputPath);
            ShowExportCompleteDialog(outputPath, items.Count, skipped, ExportFormatLabel(normalized), ExportFormatDescription(normalized));
        }

        private string ShowExportFormatDialog()
        {
            string selected = "";
            var dialog = new Window
            {
                Title = "一键导出",
                Owner = this,
                Width = 560,
                MinWidth = 520,
                SizeToContent = SizeToContent.Height,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var header = new StackPanel { Margin = new Thickness(0, 0, 0, 16) };
            header.Children.Add(new TextBlock
            {
                Text = "选择导出格式",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain")
            });
            header.Children.Add(new TextBlock
            {
                Text = "TXT 保持邮箱原格式；原始 JSON 保留 session；其它格式会调用 session_converter.py 转为 CPA/Sub2API/Cockpit/9router/Codex/AxonHub/Codex-Manager。",
                TextWrapping = TextWrapping.Wrap,
                LineHeight = 20,
                Margin = new Thickness(0, 6, 0, 0),
                Foreground = (Brush)FindResource("TextSub")
            });
            Grid.SetRow(header, 0);
            root.Children.Add(header);

            var combo = new ComboBox { SelectedIndex = 2, Margin = new Thickness(0, 0, 0, 16) };
            combo.Items.Add(new ComboBoxItem { Content = "TXT - 邮箱----密码----客户端ID----刷新令牌", Tag = "txt" });
            combo.Items.Add(new ComboBoxItem { Content = "原始 JSON - session/auth_session", Tag = "json" });
            combo.Items.Add(new ComboBoxItem { Content = "CPA JSON", Tag = "cpa" });
            combo.Items.Add(new ComboBoxItem { Content = "Sub2API JSON", Tag = "sub2api" });
            combo.Items.Add(new ComboBoxItem { Content = "Cockpit JSON", Tag = "cockpit" });
            combo.Items.Add(new ComboBoxItem { Content = "9router JSON", Tag = "9router" });
            combo.Items.Add(new ComboBoxItem { Content = "Codex auth.json", Tag = "codex" });
            combo.Items.Add(new ComboBoxItem { Content = "AxonHub JSON", Tag = "axonhub" });
            combo.Items.Add(new ComboBoxItem { Content = "Codex-Manager JSON", Tag = "codexmanager" });
            Grid.SetRow(combo, 1);
            root.Children.Add(combo);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right
            };
            var exportButton = new Button
            {
                Content = "导出",
                Width = 88,
                Style = (Style)FindResource("PrimaryButton")
            };
            exportButton.Click += (_, __) =>
            {
                selected = ((combo.SelectedItem as ComboBoxItem)?.Tag as string) ?? "cpa";
                dialog.Close();
            };
            var cancelButton = new Button
            {
                Content = "取消",
                Width = 76,
                Margin = new Thickness(8, 0, 0, 0)
            };
            cancelButton.Click += (_, __) => dialog.Close();
            actions.Children.Add(exportButton);
            actions.Children.Add(cancelButton);
            Grid.SetRow(actions, 2);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
            return selected;
        }

        private string ExportFormatLabel(string format)
        {
            string value = (format ?? "").Trim().ToLowerInvariant();
            if (value == "sub2api") return "SUB2API JSON";
            if (value == "cockpit") return "Cockpit JSON";
            if (value == "9router") return "9router JSON";
            if (value == "codex") return "Codex auth.json";
            if (value == "axonhub") return "AxonHub JSON";
            if (value == "codexmanager") return "Codex-Manager JSON";
            if (value == "json") return "原始 JSON";
            if (value == "txt") return "TXT";
            return "CPA JSON";
        }

        private string ExportFormatDescription(string format)
        {
            string value = (format ?? "").Trim().ToLowerInvariant();
            if (value == "sub2api") return "由 session_converter.py 生成的 Sub2API accounts 文档";
            if (value == "cockpit") return "由 session_converter.py 生成的 Cockpit/Codex 导入结构";
            if (value == "9router") return "由 session_converter.py 生成的 9router provider 结构";
            if (value == "codex") return "由 session_converter.py 生成的 Codex auth.json 结构";
            if (value == "axonhub") return "由 session_converter.py 生成的 AxonHub 结构；缺少 RT 时会写入占位提示";
            if (value == "codexmanager") return "由 session_converter.py 生成的 Codex-Manager 结构";
            return "由 session_converter.py 生成的 CPA JSON；缺少 id_token 时会合成兼容字段";
        }

        private void ShowExportCompleteDialog(string outputPath, int exportedCount, int skippedCount, string formatLabel, string formatDescription)
        {
            var dialog = new Window
            {
                Title = "一键导出",
                Owner = this,
                Width = 520,
                MinWidth = 460,
                SizeToContent = SizeToContent.Height,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var header = new StackPanel { Margin = new Thickness(0, 0, 0, 14) };
            header.Children.Add(new TextBlock
            {
                Text = "导出完成",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain")
            });
            header.Children.Add(new TextBlock
            {
                Text = "已生成账号 " + formatLabel + " 文件：" + formatDescription,
                TextWrapping = TextWrapping.Wrap,
                LineHeight = 20,
                Margin = new Thickness(0, 6, 0, 0),
                Foreground = (Brush)FindResource("TextSub")
            });
            Grid.SetRow(header, 0);
            root.Children.Add(header);

            var summary = new Border
            {
                Background = (Brush)FindResource("PanelBg"),
                BorderBrush = (Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(10),
                Padding = new Thickness(12),
                Margin = new Thickness(0, 0, 0, 16)
            };
            var summaryStack = new StackPanel();
            summaryStack.Children.Add(new TextBlock
            {
                Text = "数量：" + exportedCount + "    跳过：" + skippedCount,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain")
            });
            summaryStack.Children.Add(new TextBlock
            {
                Text = outputPath,
                TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 8, 0, 0),
                Foreground = (Brush)FindResource("TextSub")
            });
            summary.Child = summaryStack;
            Grid.SetRow(summary, 1);
            root.Children.Add(summary);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right
            };
            var openDirButton = new Button
            {
                Content = "打开目录",
                Width = 92,
                Style = (Style)FindResource("PrimaryButton")
            };
            openDirButton.Click += (_, __) =>
            {
                string directory = Path.GetDirectoryName(outputPath) ?? Path.Combine(rootDir, "runtime");
                OpenPath(directory);
                dialog.Close();
            };
            var closeButton = new Button
            {
                Content = "关闭",
                Width = 76,
                Margin = new Thickness(8, 0, 0, 0)
            };
            closeButton.Click += (_, __) => dialog.Close();
            actions.Children.Add(openDirButton);
            actions.Children.Add(closeButton);
            Grid.SetRow(actions, 2);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private void ShowAccountScanResultDialog(string backendOutput)
        {
            var summary = BackendResultInterpreter.TryExtractScanSummary(backendOutput);
            if (summary == null)
            {
                ShowThemedInfoDialog("账号测活", "账号测活已结束，但未解析到结果汇总。请查看下方日志确认详情。");
                return;
            }

            var results = new List<Dictionary<string, object>>();
            if (summary.TryGetValue("results", out object rawResults) && rawResults is List<object> items)
            {
                foreach (object item in items)
                {
                    if (item is Dictionary<string, object> map)
                    {
                        results.Add(map);
                    }
                }
            }

            bool directProbe = results.Any(r => BackendJson.TryGetMap(r, "probe", out _));
            var rtRows = directProbe ? new List<Dictionary<string, object>>() : results.Where(r => BackendJson.GetBool(r, "has_rt")).ToList();
            var noRtRows = directProbe ? results : results.Where(r => !BackendJson.GetBool(r, "has_rt")).ToList();

            var dialog = new Window
            {
                Title = "账号测活结果",
                Owner = this,
                Width = 740,
                MinWidth = 740,
                SizeToContent = SizeToContent.Height,
                MaxHeight = 760,
                ResizeMode = ResizeMode.CanResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var header = new StackPanel { Margin = new Thickness(0, 0, 0, 14) };
            header.Children.Add(new TextBlock
            {
                Text = "测活完成",
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain")
            });
            header.Children.Add(new TextBlock
            {
                Text = directProbe
                    ? FormatDirectProbeSummary(results, summary)
                    : "总数：" + BackendJson.GetString(summary, "total")
                        + "    正常：" + BackendJson.GetString(summary, "alive")
                        + "    掉号：" + BackendJson.GetString(summary, "account_deactivated")
                        + "    401/AT失效：" + BackendJson.GetString(summary, "at_invalid")
                        + "    手机验证：" + BackendJson.GetString(summary, "secondary_phone_verification_required")
                        + "    失败：" + BackendJson.GetString(summary, "failed"),
                Margin = new Thickness(0, 6, 0, 0),
                Foreground = (Brush)FindResource("TextSub")
            });
            Grid.SetRow(header, 0);
            root.Children.Add(header);

            var body = new StackPanel();
            if (noRtRows.Count > 0)
            {
                AddScanResultSection(body, directProbe ? "AT 测活结果" : "未接码号结果", noRtRows);
            }
            if (rtRows.Count > 0)
            {
                AddScanResultSection(body, "已接码号结果", rtRows);
            }
            if (body.Children.Count == 0)
            {
                body.Children.Add(new TextBlock
                {
                    Text = "没有可展示的测活明细。",
                    Foreground = (Brush)FindResource("TextSub")
                });
            }

            var scroll = new ScrollViewer
            {
                Content = body,
                MaxHeight = 520,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto
            };
            Grid.SetRow(scroll, 1);
            root.Children.Add(scroll);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 16, 0, 0)
            };
            var ok = new Button { Content = "关闭", Width = 82, Style = (Style)FindResource("PrimaryButton") };
            ok.Click += (_, __) => dialog.Close();
            actions.Children.Add(ok);
            Grid.SetRow(actions, 2);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private string FormatDirectProbeSummary(
            List<Dictionary<string, object>> results,
            Dictionary<string, object> summary)
        {
            results ??= new List<Dictionary<string, object>>();
            summary ??= new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            int directDeactivated = results.Count(BackendResultInterpreter.IsProbeDeactivated);
            int directOk = results.Count(row =>
                !BackendResultInterpreter.IsProbeDeactivated(row)
                && BackendResultInterpreter.IsProbeSucceeded(row));
            int direct401 = results.Count(row =>
                !BackendResultInterpreter.IsProbeDeactivated(row)
                && BackendResultInterpreter.IsProbeReturned401(row));
            int directFailed = Math.Max(0, results.Count - directOk - direct401 - directDeactivated);
            int.TryParse(BackendJson.GetString(summary, "relogin_attempted"), out int reloginAttempted);
            string directSummary = "总数：" + results.Count
                + "    AT有效：" + directOk
                + "    AT失效：" + direct401
                + "    账号停用：" + directDeactivated
                + "    其他失败：" + directFailed;
            if (reloginAttempted > 0)
            {
                directSummary += "    重登成功：" + BackendJson.GetString(summary, "relogin_success")
                    + "    重登失败：" + BackendJson.GetString(summary, "relogin_failed")
                    + "    确认停用：" + BackendJson.GetString(summary, "relogin_account_deactivated");
            }
            return directSummary;
        }

        private void AddScanResultSection(StackPanel parent, string title, List<Dictionary<string, object>> rows)
        {
            parent.Children.Add(new TextBlock
            {
                Text = title + "（" + rows.Count + "）",
                FontSize = 15,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain"),
                Margin = new Thickness(0, parent.Children.Count == 0 ? 0 : 12, 0, 8)
            });

            var card = new Border
            {
                Background = (Brush)FindResource("PanelBg"),
                BorderBrush = (Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(10),
                Padding = new Thickness(10),
                Margin = new Thickness(0, 0, 0, 4)
            };
            var stack = new StackPanel();
            foreach (Dictionary<string, object> row in rows)
            {
                string email = BackendJson.GetString(row, "email");
                string status;
                string error;
                if (BackendJson.TryGetMap(row, "probe", out var probe))
                {
                    status = BackendResultInterpreter.IsProbeDeactivated(row)
                        ? "账号停用"
                        : BackendResultInterpreter.ProbeStatusLabel(probe);
                    if (BackendJson.TryGetMap(row, "relogin", out var relogin)
                        && !BackendJson.GetBool(relogin, "ok"))
                    {
                        error = BackendJson.GetString(relogin, "error");
                    }
                    else
                    {
                        error = BackendJson.GetString(probe, "error");
                    }
                }
                else
                {
                    status = BackendResultInterpreter.ScanStatusLabel(BackendJson.GetString(row, "scan_status"));
                    error = BackendResultInterpreter.ScanResultError(row);
                }
                string line = error.Length > 0 ? email + "  ·  " + status + "  ·  " + error : email + "  ·  " + status;
                stack.Children.Add(new TextBlock
                {
                    Text = line,
                    TextWrapping = TextWrapping.Wrap,
                    LineHeight = 20,
                    Margin = new Thickness(0, 0, 0, 6),
                    Foreground = (Brush)FindResource("TextSub")
                });
            }
            card.Child = stack;
            parent.Children.Add(card);
        }

        private string ScanStatusLabel(string status)
        {
            string value = (status ?? "").Trim().ToLowerInvariant();
            return value switch
            {
                "alive" => "正常",
                "alive_probe_inconclusive" => "RT正常 / OAuth深度探测未完成",
                "account_deactivated" => "账号掉号",
                "secondary_phone_verification_required" => "手机验证",
                "phone_verification_required" => "支付完成",
                "scan_failed" => "扫描失败",
                _ => value.Length > 0 ? value : "未知"
            };
        }

        private bool AccountLivenessProbeSucceeded(Dictionary<string, object> row)
        {
            return BackendJson.TryGetMap(row, "probe", out var probe) && BackendJson.GetBool(probe, "ok");
        }

        private bool AccountLivenessProbeReturned401(Dictionary<string, object> row)
        {
            if (!BackendJson.TryGetMap(row, "probe", out var probe)) return false;
            string status = BackendJson.GetString(probe, "status").Trim().ToLowerInvariant();
            return BackendJson.GetString(probe, "status_code") == "401" || status == "token_invalid";
        }

        private bool AccountLivenessProbeDeactivated(Dictionary<string, object> row)
        {
            if (row == null) return false;
            if (AccountLivenessMapDeactivated(row)) return true;
            if (BackendJson.TryGetMap(row, "probe", out var probe)
                && AccountLivenessMapDeactivated(probe))
            {
                return true;
            }
            return BackendJson.TryGetMap(row, "relogin", out var relogin)
                && AccountLivenessMapDeactivated(relogin);
        }

        private bool AccountLivenessMapDeactivated(Dictionary<string, object> data)
        {
            if (data == null) return false;
            foreach (string key in new[] { "status", "quota_status", "account_scan_status", "error", "reason" })
            {
                string value = BackendJson.GetString(data, key).Trim();
                if (value.Contains("account_deactivated", StringComparison.OrdinalIgnoreCase)
                    || value.Contains("account_deatived", StringComparison.OrdinalIgnoreCase)
                    || value.Equals("account_deleted", StringComparison.OrdinalIgnoreCase)
                    || value.Equals("deactivated", StringComparison.OrdinalIgnoreCase)
                    || value.Contains("account has been deactivated", StringComparison.OrdinalIgnoreCase)
                    || value.Contains("deleted or deactivated", StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
            return false;
        }

        private string AccountLivenessProbeStatusLabel(Dictionary<string, object> probe)
        {
            if (AccountLivenessMapDeactivated(probe))
            {
                return "账号停用";
            }
            if (BackendJson.GetString(probe, "status_code") == "401" || BackendJson.GetString(probe, "status").Equals("token_invalid", StringComparison.OrdinalIgnoreCase))
            {
                return "AT失效 / HTTP 401";
            }
            if (BackendJson.GetBool(probe, "ok"))
            {
                string statusCode = BackendJson.GetString(probe, "status_code");
                return statusCode.Length > 0 ? "AT有效 / HTTP " + statusCode : "AT有效";
            }
            string failedCode = BackendJson.GetString(probe, "status_code");
            return failedCode.Length > 0 ? "测活失败 / HTTP " + failedCode : "测活失败";
        }

        private string ScanResultError(Dictionary<string, object> row)
        {
            foreach (string section in new[] { "oauth", "refresh" })
            {
                if (BackendJson.TryGetMap(row, section, out var map))
                {
                    string error = BackendJson.GetString(map, "error");
                    if (error.Length > 0) return error;
                }
            }
            return "";
        }

        private bool TryExtractScanSummary(string output, out Dictionary<string, object> summary)
        {
            summary = BackendResultInterpreter.TryExtractScanSummary(output);
            return summary != null;
        }

        private bool BoolValue(Dictionary<string, object> data, string key)
        {
            if (data == null || !data.TryGetValue(key, out object value) || value == null) return false;
            if (value is bool b) return b;
            string text = Convert.ToString(value)?.Trim() ?? "";
            return text.Equals("true", StringComparison.OrdinalIgnoreCase) || text == "1";
        }

        private async Task<Dictionary<string, object>> BuildAccountExportJsonAsync(PoolRow row)
        {
            if (row == null) return null;
            var data = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            if (!await TryLoadAccountDataForRowAsync(row, data) || data.Count == 0)
            {
                return null;
            }

            Dictionary<string, object> source = data;
            if (BackendJson.TryGetMap(data, "auth_session", out var authSession) && authSession.Count > 0)
            {
                source = authSession;
            }

            if (CloneExportJsonValue(source) is not Dictionary<string, object> clean || clean.Count == 0)
            {
                return null;
            }

            EnsureJsonExportEmail(clean, row);
            EnsureJsonExportRefreshToken(clean, data);
            if (IsPayPalCompletedRow(row))
            {
                SetJsonExportPlanTypePlus(clean);
            }

            return clean;
        }

        private async Task<bool> TryLoadAccountDataForRowAsync(PoolRow row, Dictionary<string, object> data)
        {
            if (row == null) return false;

            string source = (row.SourcePath ?? "").Trim();
            if (!source.EndsWith(".sqlite3", StringComparison.OrdinalIgnoreCase) || !File.Exists(source)) return false;
            try
            {
                JsonElement account = await desktopRead.ReadAccountExportAsync(
                    OnlyDigits(row.RawLine), row.Identifier);
                foreach (KeyValuePair<string, object> item in BackendJson.ElementToDictionary(account))
                {
                    data[item.Key] = item.Value;
                }
                return data.Count > 0;
            }
            catch (Exception ex)
            {
                Log("读取账号导出 backend 失败：" + SensitiveDataSanitizer.Redact(row.Identifier) + " " + SensitiveDataSanitizer.Redact(ex.Message));
                return false;
            }
        }

        private object CloneExportJsonValue(object value)
        {
            if (value is Dictionary<string, object> map)
            {
                var clean = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                foreach (var pair in map)
                {
                    clean[pair.Key] = CloneExportJsonValue(pair.Value);
                }
                return clean;
            }
            if (value is List<object> list)
            {
                return list.Select(CloneExportJsonValue).ToList();
            }
            return value;
        }

        private void EnsureJsonExportEmail(Dictionary<string, object> item, PoolRow row)
        {
            string email = (row?.Identifier ?? "").Trim();
            if (email.Length == 0) return;
            if (!BackendJson.TryGetMap(item, "user", out var user))
            {
                user = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                item["user"] = user;
            }
            if (BackendJson.GetString(user, "email").Length == 0)
            {
                user["email"] = email;
            }
        }

        private void EnsureJsonExportRefreshToken(Dictionary<string, object> item, Dictionary<string, object> sourceData)
        {
            string rt = FirstJsonString(
                BackendJson.GetString(sourceData, "oauth_refresh_token"),
                BackendJson.GetString(sourceData, "refresh_token"),
                BackendJson.NestedString(sourceData, "codex_session", "refresh_token"),
                BackendJson.NestedString(sourceData, "token", "refresh_token"),
                BackendJson.NestedString(sourceData, "credentials", "refresh_token")
            );
            item["refresh_token"] = rt;
            if (BackendJson.GetString(item, "oauth_refresh_token").Length == 0 && rt.Length > 0)
            {
                item["oauth_refresh_token"] = rt;
            }
        }

        private string NestedJsonString(Dictionary<string, object> data, string section, string key)
        {
            return BackendJson.TryGetMap(data, section, out var map) ? BackendJson.GetString(map, key) : "";
        }

        private string FirstJsonString(params string[] values)
        {
            foreach (string value in values)
            {
                string text = (value ?? "").Trim();
                if (text.Length > 0) return text;
            }
            return "";
        }

        private void SetJsonExportPlanTypePlus(Dictionary<string, object> item)
        {
            if (item.ContainsKey("planType"))
            {
                item["planType"] = "plus";
            }
            if (!BackendJson.TryGetMap(item, "account", out var account))
            {
                account = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                item["account"] = account;
            }
            account["planType"] = "plus";
        }

        private string JsonExportDedupKey(Dictionary<string, object> item, PoolRow row)
        {
            if (BackendJson.TryGetMap(item, "user", out var user))
            {
                string userEmail = BackendJson.GetString(user, "email").Trim();
                if (userEmail.Length > 0) return userEmail.ToLowerInvariant();
            }
            string email = BackendJson.GetString(item, "email").Trim();
            if (email.Length > 0) return email.ToLowerInvariant();
            email = (row?.Identifier ?? "").Trim();
            if (email.Length > 0) return email.ToLowerInvariant();
            return JsonSerializer.Serialize(item);
        }

        private bool TryBuildAccountExportLine(PoolRow row, out string line)
        {
            line = "";
            if (row == null) return false;

            string source = FindMailboxLineForRow(row);
            if (source.Length == 0 && !string.IsNullOrWhiteSpace(row.RawLine))
            {
                source = row.RawLine;
            }

            if (!TryParseMailboxExportParts(source, row, out string email, out string password, out string clientId, out string refreshToken))
            {
                return false;
            }

            if (email.Length == 0 || password.Length == 0 || clientId.Length == 0 || refreshToken.Length == 0)
            {
                return false;
            }

            line = email + "----" + password + "----" + clientId + "----" + refreshToken;
            return true;
        }

        private bool TryParseMailboxExportParts(string source, PoolRow row, out string email, out string password, out string clientId, out string refreshToken)
        {
            email = "";
            password = "";
            clientId = "";
            refreshToken = "";

            string value = (source ?? "").Trim().TrimStart('\ufeff');
            if (value.Length == 0 || value.StartsWith("#")) return false;
            if (value.StartsWith("cfworker://", StringComparison.OrdinalIgnoreCase)
                || value.EndsWith("@edu.liziai.cloud", StringComparison.OrdinalIgnoreCase)
                || value.EndsWith("@liziai.cloud", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }

            if (value.Contains("----"))
            {
                string[] parts = value.Split(new[] { "----" }, StringSplitOptions.None);
                if (parts.Length < 4) return false;
                email = parts[0].Trim();
                password = parts[1].Trim();
                string p2 = parts[2].Trim();
                string p3 = string.Join("----", parts.Skip(3)).Trim();
                clientId = LooksMicrosoftClientId(p2) || !LooksMicrosoftClientId(p3) ? p2 : p3;
                refreshToken = LooksMicrosoftClientId(p2) || !LooksMicrosoftClientId(p3) ? p3 : p2;
                return true;
            }

            if (value.Contains("---"))
            {
                string[] parts = value.Split(new[] { "---" }, StringSplitOptions.None);
                if (parts.Length < 3) return false;
                email = parts[0].Trim();
                password = parts[1].Trim();
                clientId = !string.IsNullOrWhiteSpace(row?.ClientId) ? row.ClientId.Trim() : DefaultMailboxClientId();
                refreshToken = parts[2].Trim();
                return true;
            }

            return false;
        }

        private bool LooksMicrosoftClientId(string value)
        {
            return Regex.IsMatch((value ?? "").Trim(), "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$");
        }

        private string DefaultMailboxClientId()
        {
            string configured = settingsService.GetString("email_registration.oauth_client_id").Trim();
            return configured.Length > 0 ? configured : "9e5f94bc-e8a4-4e73-b8be-63364c29d753";
        }

        private string ShowImportTargetDialog(string title)
        {
            string selected = "";
            var dialog = new Window
            {
                Title = title,
                Owner = this,
                Width = 360,
                Height = 190,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var label = new TextBlock
            {
                Text = "选择导入目标",
                Foreground = (System.Windows.Media.Brush)FindResource("TextMain"),
                FontWeight = FontWeights.SemiBold,
                Margin = new Thickness(0, 0, 0, 10)
            };
            Grid.SetRow(label, 0);
            root.Children.Add(label);

            var combo = new ComboBox { SelectedIndex = 0, Margin = new Thickness(0, 0, 0, 18) };
            combo.Items.Add(new ComboBoxItem { Content = "CPA", Tag = "cpa" });
            combo.Items.Add(new ComboBoxItem { Content = "SUB2API", Tag = "sub2api" });
            Grid.SetRow(combo, 1);
            root.Children.Add(combo);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right
            };
            var ok = new Button { Content = "确定", Width = 76, Style = (Style)FindResource("PrimaryButton") };
            ok.Click += (_, __) =>
            {
                selected = ((combo.SelectedItem as ComboBoxItem)?.Tag as string) ?? "cpa";
                dialog.Close();
            };
            var cancel = new Button { Content = "取消", Width = 76, Margin = new Thickness(8, 0, 0, 0) };
            cancel.Click += (_, __) =>
            {
                selected = "";
                dialog.Close();
            };
            actions.Children.Add(ok);
            actions.Children.Add(cancel);
            Grid.SetRow(actions, 2);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
            return selected;
        }

        private void AddImportTargetArg(List<string> args, string target)
        {
            args.Add("--import-target");
            string value = (target ?? "").Trim().ToLowerInvariant();
            if (value == "sub2api")
            {
                args.Add("sub2api");
            }
            else if (value == "cliproxyapi")
            {
                args.Add("cliproxyapi");
            }
            else
            {
                args.Add("cpa");
            }
        }

        private string ImportTargetLabel(string target)
        {
            string value = (target ?? "").Trim().ToLowerInvariant();
            if (value == "sub2api") return "SUB2API";
            if (value == "cliproxyapi") return "CLIProxyAPI";
            return "CPA";
        }
    }
}
