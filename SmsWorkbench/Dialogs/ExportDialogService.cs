// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// The dialogs the import/export actions open.
    /// </summary>
    /// <remarks>
    /// Split from <c>MainWindow.Export.cs</c> so that partial only owns the export
    /// flow (collect, write, convert). Every method takes the owner window: WPF
    /// resolves brushes and ownership through it, and that is the only thing these
    /// dialogs need from the window.
    /// </remarks>
    public interface IExportDialogService
    {
        /// <summary>Format picker; empty string when the operator cancels.</summary>
        string ShowExportFormat(Window owner);

        /// <summary>"Export finished" summary with an "open folder" action.</summary>
        void ShowExportComplete(
            Window owner,
            string outputPath,
            int exportedCount,
            int skippedCount,
            string formatLabel,
            string formatDescription);

        /// <summary>
        /// Account-batch result panel. Returns <c>false</c> when the backend output
        /// carried no summary, so the caller can report that with its own dialog.
        /// </summary>
        bool ShowAccountScanResult(Window owner, string backendOutput, BackendAccountResultKind resultKind);

        /// <summary>CPA / SUB2API import target picker; empty string on cancel.</summary>
        string ShowImportTarget(Window owner, string title);
    }

    public sealed class ExportDialogService : IExportDialogService
    {
        private readonly Action<string> openPath;
        private readonly Func<string> runtimeDirectory;

        public ExportDialogService(Action<string> openPath, Func<string> runtimeDirectory)
        {
            this.openPath = openPath;
            this.runtimeDirectory = runtimeDirectory;
        }

        public string ShowExportFormat(Window owner)
        {
            string selected = "";
            var dialog = new Window
            {
                Title = Loc.T("一键导出"),
                Owner = owner,
                Width = 560,
                MinWidth = 520,
                SizeToContent = SizeToContent.Height,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)owner.FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var header = new StackPanel { Margin = new Thickness(0, 0, 0, 16) };
            header.Children.Add(new TextBlock
            {
                Text = Loc.T("选择导出格式"),
                FontSize = (double)owner.FindResource("Type.Title.Size"),
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)owner.FindResource("TextMain")
            });
            header.Children.Add(new TextBlock
            {
                Text = Loc.T("TXT 保持邮箱原格式；原始 JSON 保留 session；其它格式会调用 session_converter.py 转为 CPA/Sub2API/Cockpit/9router/Codex/AxonHub/Codex-Manager。"),
                TextWrapping = TextWrapping.Wrap,
                LineHeight = 20,
                Margin = new Thickness(0, 6, 0, 0),
                Foreground = (Brush)owner.FindResource("TextSub")
            });
            Grid.SetRow(header, 0);
            root.Children.Add(header);

            var combo = new ComboBox { SelectedIndex = 2, Margin = new Thickness(0, 0, 0, 16) };
            combo.Items.Add(new ComboBoxItem { Content = Loc.T("TXT - 邮箱----密码----客户端ID----刷新令牌"), Tag = "txt" });
            combo.Items.Add(new ComboBoxItem { Content = Loc.T("原始 JSON - session/auth_session"), Tag = "json" });
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
                Content = Loc.T("导出"),
                Width = 88,
                Style = (Style)owner.FindResource("PrimaryButton")
            };
            exportButton.Click += (_, __) =>
            {
                selected = ((combo.SelectedItem as ComboBoxItem)?.Tag as string) ?? "cpa";
                dialog.Close();
            };
            var cancelButton = new Button
            {
                Content = Loc.T("取消"),
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

        public void ShowExportComplete(
            Window owner,
            string outputPath,
            int exportedCount,
            int skippedCount,
            string formatLabel,
            string formatDescription)
        {
            var dialog = new Window
            {
                Title = Loc.T("一键导出"),
                Owner = owner,
                Width = 520,
                MinWidth = 460,
                SizeToContent = SizeToContent.Height,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)owner.FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var header = new StackPanel { Margin = new Thickness(0, 0, 0, 14) };
            header.Children.Add(new TextBlock
            {
                Text = Loc.T("导出完成"),
                FontSize = (double)owner.FindResource("Type.Title.Size"),
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)owner.FindResource("TextMain")
            });
            header.Children.Add(new TextBlock
            {
                Text = Loc.F("已生成账号 {0} 文件：{1}", formatLabel, formatDescription),
                TextWrapping = TextWrapping.Wrap,
                LineHeight = 20,
                Margin = new Thickness(0, 6, 0, 0),
                Foreground = (Brush)owner.FindResource("TextSub")
            });
            Grid.SetRow(header, 0);
            root.Children.Add(header);

            var summary = new Border
            {
                Background = (Brush)owner.FindResource("PanelBg"),
                BorderBrush = (Brush)owner.FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = (CornerRadius)owner.FindResource("Corner.Lg"),
                Padding = new Thickness(12),
                Margin = new Thickness(0, 0, 0, 16)
            };
            var summaryStack = new StackPanel();
            summaryStack.Children.Add(new TextBlock
            {
                Text = Loc.F("数量：{0}    跳过：{1}", exportedCount, skippedCount),
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)owner.FindResource("TextMain")
            });
            summaryStack.Children.Add(new TextBlock
            {
                Text = outputPath,
                TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 8, 0, 0),
                Foreground = (Brush)owner.FindResource("TextSub")
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
                Content = Loc.T("打开目录"),
                Width = 92,
                Style = (Style)owner.FindResource("PrimaryButton")
            };
            openDirButton.Click += (_, __) =>
            {
                string directory = Path.GetDirectoryName(outputPath) ?? runtimeDirectory();
                openPath(directory);
                dialog.Close();
            };
            var closeButton = new Button
            {
                Content = Loc.T("关闭"),
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

        public bool ShowAccountScanResult(Window owner, string backendOutput, BackendAccountResultKind resultKind)
        {
            string title = BackendResultInterpreter.AccountResultTitle(resultKind);
            var summary = BackendResultInterpreter.TryExtractScanSummary(backendOutput);
            if (summary == null) return false;

            var results = new List<Dictionary<string, object>>();
            if (summary.TryGetValue("results", out object? rawResults) && rawResults is List<object> items)
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
            // Promotion rows carry their own badge; without this the panel would
            // show "AT有效 / HTTP 200" and hide the actual 优惠 answer.
            bool isPromotion = BackendResultInterpreter.IsPromotionRows(results);
            // 支付资格 rows carry their own badge as well; the liveness rendering
            // would otherwise label an "无可用方式" row as an AT success.
            bool isQualification = !isPromotion && BackendResultInterpreter.IsPaymentQualificationRows(results);
            var rtRows = directProbe ? new List<Dictionary<string, object>>() : results.Where(r => BackendJson.GetBool(r, "has_rt")).ToList();
            var noRtRows = directProbe ? results : results.Where(r => !BackendJson.GetBool(r, "has_rt")).ToList();

            var dialog = new Window
            {
                Title = Loc.F("{0}结果", title),
                Owner = owner,
                Width = 740,
                MinWidth = 740,
                SizeToContent = SizeToContent.Height,
                MaxHeight = 760,
                ResizeMode = ResizeMode.CanResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)owner.FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var header = new StackPanel { Margin = new Thickness(0, 0, 0, 14) };
            header.Children.Add(new TextBlock
            {
                Text = Loc.F("{0}完成", title),
                FontSize = (double)owner.FindResource("Type.Title.Size"),
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)owner.FindResource("TextMain")
            });
            header.Children.Add(new TextBlock
            {
                Text = isQualification
                    ? BackendResultInterpreter.PaymentQualificationSummary(results)
                    : isPromotion
                    ? BackendResultInterpreter.PromotionSummary(results)
                    : directProbe
                    ? ExportViewModel.FormatDirectProbeSummary(results, summary)
                    : "总数：" + BackendJson.GetString(summary, "total")
                        + "    正常：" + BackendJson.GetString(summary, "alive")
                        + "    掉号：" + BackendJson.GetString(summary, "account_deactivated")
                        + "    401/AT失效：" + BackendJson.GetString(summary, "at_invalid")
                        + "    手机验证：" + BackendJson.GetString(summary, "secondary_phone_verification_required")
                        + "    失败：" + BackendJson.GetString(summary, "failed"),
                Margin = new Thickness(0, 6, 0, 0),
                Foreground = (Brush)owner.FindResource("TextSub")
            });
            Grid.SetRow(header, 0);
            root.Children.Add(header);

            var body = new StackPanel();
            if (noRtRows.Count > 0)
            {
                AddScanResultSection(
                    owner,
                    body,
                    isPromotion ? title + "结果" : (directProbe ? "AT 测活结果" : "未接码号结果"),
                    noRtRows);
            }
            if (rtRows.Count > 0)
            {
                AddScanResultSection(owner, body, "已接码号结果", rtRows);
            }
            if (body.Children.Count == 0)
            {
                body.Children.Add(new TextBlock
                {
                    Text = Loc.F("没有可展示的{0}明细。", title),
                    Foreground = (Brush)owner.FindResource("TextSub")
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
            var ok = new Button
            {
                Content = Loc.T("关闭"),
                Width = 82,
                Style = (Style)owner.FindResource("PrimaryButton")
            };
            ok.Click += (_, __) => dialog.Close();
            actions.Children.Add(ok);
            Grid.SetRow(actions, 2);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
            return true;
        }

        private static void AddScanResultSection(
            Window owner,
            StackPanel parent,
            string title,
            List<Dictionary<string, object>> rows)
        {
            parent.Children.Add(new TextBlock
            {
                Text = title + "（" + rows.Count + "）",
                FontSize = (double)owner.FindResource("Type.Subtitle.Size"),
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)owner.FindResource("TextMain"),
                Margin = new Thickness(0, parent.Children.Count == 0 ? 0 : 12, 0, 8)
            });

            var card = new Border
            {
                Background = (Brush)owner.FindResource("PanelBg"),
                BorderBrush = (Brush)owner.FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = (CornerRadius)owner.FindResource("Corner.Lg"),
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
                    status = BackendResultInterpreter.ResultRowStatus(row);
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
                else if (BackendJson.GetString(row, "error_code") == "account_not_found")
                {
                    status = "支付资格未知";
                    error = "本地账号不存在";
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
                    Foreground = (Brush)owner.FindResource("TextSub")
                });
            }
            card.Child = stack;
            parent.Children.Add(card);
        }

        public string ShowImportTarget(Window owner, string title)
        {
            string selected = "";
            var dialog = new Window
            {
                Title = title,
                Owner = owner,
                Width = 360,
                Height = 190,
                ResizeMode = ResizeMode.NoResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)owner.FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(18) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var label = new TextBlock
            {
                Text = Loc.T("选择导入目标"),
                Foreground = (Brush)owner.FindResource("TextMain"),
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
            var ok = new Button
            {
                Content = Loc.T("确定"),
                Width = 76,
                Style = (Style)owner.FindResource("PrimaryButton")
            };
            ok.Click += (_, __) =>
            {
                selected = ((combo.SelectedItem as ComboBoxItem)?.Tag as string) ?? "cpa";
                dialog.Close();
            };
            var cancel = new Button { Content = Loc.T("取消"), Width = 76, Margin = new Thickness(8, 0, 0, 0) };
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
    }
}
