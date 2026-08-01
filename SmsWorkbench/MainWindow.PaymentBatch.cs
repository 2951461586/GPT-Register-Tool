namespace SmsWorkbench
{
    public partial class MainWindow
    {
        private void BatchProtocolPayment_Click(object sender, RoutedEventArgs e)
        {
            var rows = SelectedEmailRowsOrNotify("批量协议支付");
            if (rows.Count == 0) return;
            ShowPaymentBatchDialog(rows);
        }

        private void ShowPaymentBatchDialog(IEnumerable<PoolRow> sourceRows)
        {
            var rows = (sourceRows ?? Enumerable.Empty<PoolRow>())
                .Where(row => !string.IsNullOrWhiteSpace(row.Identifier))
                .GroupBy(row => row.Identifier.Trim().ToLowerInvariant())
                .Select(group => group.First())
                .ToList();
            if (rows.Count == 0)
            {
                ShowEmailSelectionRequired("批量协议支付");
                return;
            }

            var win = new Window
            {
                Title = "批量协议支付",
                Owner = this,
                Width = Math.Min(1120, SystemParameters.WorkArea.Width - 80),
                Height = Math.Min(820, SystemParameters.WorkArea.Height - 80),
                MinWidth = 920,
                MinHeight = 680,
                ResizeMode = ResizeMode.CanResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(20) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(220) });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var header = new Grid { Margin = new Thickness(0, 0, 0, 14) };
            header.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            header.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            header.Children.Add(new TextBlock
            {
                Text = "批量协议支付",
                FontSize = 20,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain"),
                VerticalAlignment = VerticalAlignment.Center
            });
            var accountSummary = new TextBlock
            {
                Text = $"账号 {rows.Count}  ·  AT 已获取 {rows.Count(row => row.HasAccessToken)}",
                Foreground = (Brush)FindResource("TextSub"),
                VerticalAlignment = VerticalAlignment.Center
            };
            Grid.SetColumn(accountSummary, 1);
            header.Children.Add(accountSummary);
            Grid.SetRow(header, 0);
            root.Children.Add(header);

            var settings = new Grid { Margin = new Thickness(0, 0, 0, 14) };
            for (int i = 0; i < 4; i++) settings.ColumnDefinitions.Add(new ColumnDefinition());
            settings.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            settings.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            settings.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            StackPanel Field(string label, Control control, int column, int row, int columnSpan = 1)
            {
                var panel = new StackPanel { Margin = new Thickness(column == 0 ? 0 : 7, 0, column == 3 ? 0 : 7, 10) };
                panel.Children.Add(new TextBlock
                {
                    Text = label,
                    FontSize = 12,
                    Foreground = (Brush)FindResource("TextSub"),
                    Margin = new Thickness(0, 0, 0, 4)
                });
                control.MinHeight = 32;
                panel.Children.Add(control);
                Grid.SetColumn(panel, column);
                Grid.SetColumnSpan(panel, columnSpan);
                Grid.SetRow(panel, row);
                settings.Children.Add(panel);
                return panel;
            }

            var methodCombo = new ComboBox();
            AddPaymentMethodItems(methodCombo);
            methodCombo.SelectedIndex = 8;
            Field("支付方式", methodCombo, 0, 0);

            var workersCombo = new ComboBox { ItemsSource = Enumerable.Range(1, 10).ToList(), SelectedItem = 2 };
            Field("并发", workersCombo, 1, 0);
            var retriesCombo = new ComboBox { ItemsSource = new[] { 0, 1, 2 }, SelectedItem = 1 };
            Field("瞬态重试", retriesCombo, 2, 0);
            var canaryBox = new TextBox { Text = "0" };
            Field("Canary 数量（0=全部）", canaryBox, 3, 0);

            string AutoBatchId(string method) => method + "_" + DateTime.Now.ToString("yyyyMMdd_HHmmss");
            var batchIdBox = new TextBox { Text = AutoBatchId("momo") };
            Field("批次 ID（复用同一 ID 可断点继续）", batchIdBox, 0, 1, 2);
            var proxyBox = new TextBox { Text = "", FontFamily = new FontFamily("Consolas") };
            Field("代理 Seed（留空使用协议代理池）", proxyBox, 2, 1, 2);

            var options = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 2, 0, 2) };
            var jitCheck = new CheckBox { Content = "401 时邮箱 OTP OAuth 新 AT", IsChecked = true, Margin = new Thickness(0, 0, 22, 0) };
            var probeOnlyCheck = new CheckBox { Content = "仅探测资格", IsChecked = false, Margin = new Thickness(0, 0, 22, 0) };
            var requireZeroCheck = new CheckBox { Content = "要求 0 元金额", IsChecked = true };
            options.Children.Add(jitCheck);
            options.Children.Add(probeOnlyCheck);
            options.Children.Add(requireZeroCheck);
            Grid.SetRow(options, 2);
            Grid.SetColumnSpan(options, 4);
            settings.Children.Add(options);
            Grid.SetRow(settings, 1);
            root.Children.Add(settings);

            var matrixRows = LoadConfiguredPaymentMatrix("momo");
            if (matrixRows.Count == 0) matrixRows.Add(DefaultPaymentMatrixRow("momo"));
            var matrixShell = new Grid { Margin = new Thickness(0, 0, 0, 14) };
            matrixShell.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            matrixShell.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            var matrixHeader = new Grid { Margin = new Thickness(0, 0, 0, 6) };
            matrixHeader.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            matrixHeader.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            matrixHeader.Children.Add(new TextBlock
            {
                Text = "账号地区 / 支付资格矩阵",
                FontSize = 13,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain")
            });
            var matrixActions = new StackPanel { Orientation = Orientation.Horizontal };
            var addCellButton = new Button { Content = "添加单元", MinWidth = 82, Height = 28, Margin = new Thickness(0, 0, 6, 0) };
            var deleteCellButton = new Button { Content = "删除单元", MinWidth = 82, Height = 28 };
            matrixActions.Children.Add(addCellButton);
            matrixActions.Children.Add(deleteCellButton);
            Grid.SetColumn(matrixActions, 1);
            matrixHeader.Children.Add(matrixActions);
            matrixShell.Children.Add(matrixHeader);

            var matrixGrid = new DataGrid
            {
                AutoGenerateColumns = false,
                ItemsSource = matrixRows,
                CanUserAddRows = false,
                CanUserDeleteRows = false,
                HeadersVisibility = DataGridHeadersVisibility.Column,
                GridLinesVisibility = DataGridGridLinesVisibility.Horizontal,
                Background = (Brush)FindResource("PanelBg"),
                BorderBrush = (Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                RowHeight = 34
            };
            matrixGrid.Columns.Add(new DataGridTextColumn { Header = "名称", Binding = new Binding("Name"), Width = 120 });
            matrixGrid.Columns.Add(new DataGridTextColumn { Header = "注册区", Binding = new Binding("RegistrationCountry"), Width = 62 });
            matrixGrid.Columns.Add(new DataGridTextColumn { Header = "Checkout", Binding = new Binding("CheckoutCountry"), Width = 78 });
            matrixGrid.Columns.Add(new DataGridTextColumn { Header = "Promotion", Binding = new Binding("PromotionCountry"), Width = 82 });
            matrixGrid.Columns.Add(new DataGridTextColumn { Header = "Provider", Binding = new Binding("ProviderCountry"), Width = 76 });
            matrixGrid.Columns.Add(new DataGridTextColumn { Header = "Approve", Binding = new Binding("ApproveCountry"), Width = 72 });
            matrixGrid.Columns.Add(new DataGridTextColumn { Header = "Redirect", Binding = new Binding("RedirectCountry"), Width = 72 });
            matrixGrid.Columns.Add(new DataGridTextColumn { Header = "策略", Binding = new Binding("Strategy"), Width = new DataGridLength(1, DataGridLengthUnitType.Star) });
            matrixGrid.Columns.Add(new DataGridTextColumn { Header = "样本", Binding = new Binding("SampleSize"), Width = 56 });
            Grid.SetRow(matrixGrid, 1);
            matrixShell.Children.Add(matrixGrid);
            Grid.SetRow(matrixShell, 2);
            root.Children.Add(matrixShell);

            var results = new ObservableCollection<PaymentBatchResultRow>();
            var resultGrid = new DataGrid
            {
                AutoGenerateColumns = false,
                ItemsSource = results,
                IsReadOnly = true,
                HeadersVisibility = DataGridHeadersVisibility.Column,
                GridLinesVisibility = DataGridGridLinesVisibility.Horizontal,
                Background = (Brush)FindResource("PanelBg"),
                BorderBrush = (Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                RowHeight = 32,
                Margin = new Thickness(0, 0, 0, 12)
            };
            resultGrid.Columns.Add(new DataGridTextColumn { Header = "账号", Binding = new Binding("AccountRef"), Width = 135 });
            resultGrid.Columns.Add(new DataGridTextColumn { Header = "矩阵", Binding = new Binding("MatrixCell"), Width = 115 });
            resultGrid.Columns.Add(new DataGridTextColumn { Header = "AT", Binding = new Binding("AuthStatus"), Width = 68 });
            resultGrid.Columns.Add(new DataGridTextColumn { Header = "JIT", Binding = new Binding("RefreshStatus"), Width = 66 });
            resultGrid.Columns.Add(new DataGridTextColumn { Header = "资格", Binding = new Binding("Eligibility"), Width = 66 });
            resultGrid.Columns.Add(new DataGridTextColumn { Header = "结果", Binding = new Binding("Decision"), Width = new DataGridLength(1, DataGridLengthUnitType.Star) });
            resultGrid.Columns.Add(new DataGridTextColumn { Header = "次数", Binding = new Binding("Attempts"), Width = 56 });
            Grid.SetRow(resultGrid, 3);
            root.Children.Add(resultGrid);

            var footer = new Grid();
            footer.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            footer.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var statusText = new TextBlock
            {
                Text = "就绪",
                Foreground = (Brush)FindResource("TextSub"),
                VerticalAlignment = VerticalAlignment.Center,
                TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 0, 16, 0)
            };
            footer.Children.Add(statusText);
            var buttons = new StackPanel { Orientation = Orientation.Horizontal };
            var openReportButton = new Button { Content = "打开报告", MinWidth = 84, Height = 32, IsEnabled = false, Margin = new Thickness(0, 0, 8, 0) };
            var runButton = new Button { Content = "开始执行", MinWidth = 96, Height = 32, Style = (Style)FindResource("PrimaryButton"), Margin = new Thickness(0, 0, 8, 0) };
            var closeButton = new Button { Content = "关闭", MinWidth = 72, Height = 32 };
            buttons.Children.Add(openReportButton);
            buttons.Children.Add(runButton);
            buttons.Children.Add(closeButton);
            Grid.SetColumn(buttons, 1);
            footer.Children.Add(buttons);
            Grid.SetRow(footer, 4);
            root.Children.Add(footer);

            string currentReportPath = "";
            string previousAutoId = batchIdBox.Text;

            string SelectedMethod() => methodCombo.SelectedItem is ComboBoxItem item
                ? NormalizePaymentMethod(Convert.ToString(item.Tag) ?? "paypal")
                : "paypal";

            methodCombo.SelectionChanged += (_, __) =>
            {
                string method = SelectedMethod();
                if (batchIdBox.Text == previousAutoId || string.IsNullOrWhiteSpace(batchIdBox.Text))
                {
                    previousAutoId = AutoBatchId(method);
                    batchIdBox.Text = previousAutoId;
                }
                var configured = LoadConfiguredPaymentMatrix(method);
                matrixRows.Clear();
                IEnumerable<PaymentMatrixRow> cells = configured.Count > 0
                    ? configured
                    : new[] { DefaultPaymentMatrixRow(method) };
                foreach (var cell in cells)
                    matrixRows.Add(cell);
            };
            addCellButton.Click += (_, __) => matrixRows.Add(DefaultPaymentMatrixRow(SelectedMethod()));
            deleteCellButton.Click += (_, __) =>
            {
                if (matrixGrid.SelectedItem is PaymentMatrixRow selected && matrixRows.Count > 1)
                    matrixRows.Remove(selected);
            };
            probeOnlyCheck.Checked += (_, __) => requireZeroCheck.IsEnabled = false;
            probeOnlyCheck.Unchecked += (_, __) => requireZeroCheck.IsEnabled = true;

            runButton.Click += async (_, __) =>
            {
                matrixGrid.CommitEdit(DataGridEditingUnit.Cell, true);
                matrixGrid.CommitEdit(DataGridEditingUnit.Row, true);
                int workers = workersCombo.SelectedItem is int workerValue ? workerValue : 1;
                int retries = retriesCombo.SelectedItem is int retryValue ? retryValue : 1;
                if (!int.TryParse(canaryBox.Text.Trim(), out int canary) || canary < 0)
                {
                    statusText.Text = "Canary 数量必须是非负整数。";
                    return;
                }
                string batchId = Regex.Replace(batchIdBox.Text.Trim(), @"[^A-Za-z0-9_.-]+", "_");
                if (batchId.Length == 0)
                {
                    statusText.Text = "请输入批次 ID。";
                    return;
                }
                if (matrixRows.Any(cell => !cell.IsValid()))
                {
                    statusText.Text = "矩阵国家代码必须为空或两位字母，样本数必须大于 0。";
                    return;
                }

                string emailFile = Path.Combine(Path.GetTempPath(), "payment_batch_" + Guid.NewGuid().ToString("N") + ".txt");
                string matrixFile = Path.Combine(Path.GetTempPath(), "payment_matrix_" + Guid.NewGuid().ToString("N") + ".json");
                try
                {
                    File.WriteAllLines(emailFile, rows.Select(row => row.Identifier.Trim()), new UTF8Encoding(false));
                    File.WriteAllText(matrixFile, SerializePaymentMatrix(matrixRows, SelectedMethod()), new UTF8Encoding(false));
                    var args = new List<string>
                    {
                        "--extract-payment-link", "--payment-method", SelectedMethod(),
                        "--email-file", emailFile, "--workers", workers.ToString(),
                        "--payment-batch-id", batchId, "--payment-retries", retries.ToString(),
                        "--payment-matrix", matrixFile, "--refresh-timeout", "180"
                    };
                    if (jitCheck.IsChecked != true) args.Add("--no-jit-at-refresh");
                    if (probeOnlyCheck.IsChecked == true) args.Add("--payment-probe-only");
                    if (requireZeroCheck.IsChecked != true) args.Add("--no-require-zero");
                    if (canary > 0) args.AddRange(new[] { "--payment-canary", canary.ToString() });
                    if (!string.IsNullOrWhiteSpace(proxyBox.Text)) args.AddRange(new[] { "--proxy", proxyBox.Text.Trim() });

                    results.Clear();
                    statusText.Text = "正在执行 JIT 探测与协议支付批次...";
                    runButton.IsEnabled = false;
                    int waveCount = Math.Max(1, (int)Math.Ceiling((canary > 0 ? Math.Min(canary, rows.Count) : rows.Count) / (double)Math.Max(1, workers)));
                    int timeout = (int)Math.Min(12L * 60 * 60 * 1000, Math.Max(120000L, (long)ProtocolPaymentBackendTimeoutMs(SelectedMethod()) * waveCount));
                    string raw = await Task.Run(() => RunBackendWithResult("批量协议支付", args, timeout));
                    using JsonDocument document = JsonDocument.Parse(raw);
                    JsonElement report = document.RootElement;
                    PopulatePaymentBatchResults(report, results);
                    currentReportPath = BatchJsonString(report, "report_path");
                    openReportButton.IsEnabled = currentReportPath.Length > 0 && File.Exists(currentReportPath);
                    statusText.Text = FormatPaymentBatchSummary(report);
                    RefreshPools();
                }
                catch (Exception ex)
                {
                    statusText.Text = "执行失败：" + ex.Message;
                }
                finally
                {
                    runButton.IsEnabled = true;
                    TryDeleteTemporaryFile(emailFile);
                    TryDeleteTemporaryFile(matrixFile);
                }
            };

            openReportButton.Click += (_, __) =>
            {
                if (currentReportPath.Length > 0) OpenPath(currentReportPath);
            };
            closeButton.Click += (_, __) => win.Close();
            win.Content = root;
            win.ShowDialog();
        }

        private ObservableCollection<PaymentMatrixRow> LoadConfiguredPaymentMatrix(string method)
        {
            var output = new ObservableCollection<PaymentMatrixRow>();
            try
            {
                var config = ReadJsonObject(Path.Combine(rootDir, "config.json"));
                var protocol = GetSection(config, "protocol_payments");
                var matrix = GetChildSection(protocol, "matrix");
                if (!matrix.TryGetValue("cells", out object raw) || raw is not List<object> cells) return output;
                foreach (object value in cells)
                {
                    if (value is not Dictionary<string, object> cell) continue;
                    string cellMethod = NormalizePaymentMethod(GetString(cell, "payment_method"));
                    if (GetString(cell, "payment_method").Length > 0 && cellMethod != NormalizePaymentMethod(method)) continue;
                    output.Add(new PaymentMatrixRow
                    {
                        Name = FirstNonEmpty(GetString(cell, "name"), "cell_" + (output.Count + 1)),
                        RegistrationCountry = GetString(cell, "registration_country"),
                        CheckoutCountry = GetString(cell, "checkout_country"),
                        PromotionCountry = GetString(cell, "promotion_country"),
                        ProviderCountry = GetString(cell, "provider_country"),
                        ApproveCountry = GetString(cell, "approve_country"),
                        RedirectCountry = GetString(cell, "redirect_country"),
                        Strategy = GetString(cell, "strategy"),
                        SampleSize = int.TryParse(GetString(cell, "sample_size"), out int sample) ? Math.Max(1, sample) : 1
                    });
                }
            }
            catch { }
            return output;
        }

        private static PaymentMatrixRow DefaultPaymentMatrixRow(string method)
        {
            string normalized = (method ?? "").Trim().ToLowerInvariant();
            string country = normalized == "momo" ? "VN" : normalized == "kakao" ? "KR" : "";
            return new PaymentMatrixRow
            {
                Name = country.Length > 0 ? country.ToLowerInvariant() + "_sticky" : "default",
                CheckoutCountry = country,
                PromotionCountry = normalized == "kakao" ? "VN" : country,
                ProviderCountry = country,
                ApproveCountry = country,
                RedirectCountry = country,
                Strategy = normalized == "momo" ? "custom_promo" : "",
                SampleSize = 5
            };
        }

        private static string SerializePaymentMatrix(IEnumerable<PaymentMatrixRow> rows, string method)
        {
            var cells = rows.Select(row => new Dictionary<string, object>
            {
                ["name"] = row.Name.Trim(),
                ["payment_method"] = method,
                ["registration_country"] = row.RegistrationCountry.Trim().ToUpperInvariant(),
                ["checkout_country"] = row.CheckoutCountry.Trim().ToUpperInvariant(),
                ["promotion_country"] = row.PromotionCountry.Trim().ToUpperInvariant(),
                ["provider_country"] = row.ProviderCountry.Trim().ToUpperInvariant(),
                ["approve_country"] = row.ApproveCountry.Trim().ToUpperInvariant(),
                ["redirect_country"] = row.RedirectCountry.Trim().ToUpperInvariant(),
                ["strategy"] = row.Strategy.Trim(),
                ["sample_size"] = Math.Max(1, row.SampleSize)
            }).Cast<object>().ToList();
            return JsonSerializer.Serialize(new Dictionary<string, object> { ["cells"] = cells }, new JsonSerializerOptions { WriteIndented = true });
        }

        private static void PopulatePaymentBatchResults(JsonElement report, ObservableCollection<PaymentBatchResultRow> output)
        {
            if (!report.TryGetProperty("results", out JsonElement values) || values.ValueKind != JsonValueKind.Array) return;
            foreach (JsonElement row in values.EnumerateArray())
            {
                bool authenticated = JsonBool(row, "authenticated");
                bool refreshed = JsonBool(row, "refreshed");
                string eligibility = "未知";
                if (row.TryGetProperty("eligible", out JsonElement eligible) && eligible.ValueKind is JsonValueKind.True or JsonValueKind.False)
                    eligibility = eligible.GetBoolean() ? "符合" : "不符合";
                output.Add(new PaymentBatchResultRow
                {
                    AccountRef = BatchJsonString(row, "account_ref"),
                    MatrixCell = BatchJsonString(row, "matrix_cell"),
                    AuthStatus = authenticated ? "200" : "失败",
                    RefreshStatus = refreshed ? "已刷新" : "未刷新",
                    Eligibility = eligibility,
                    Decision = BatchJsonString(row, "decision").Length > 0
                        ? BatchJsonString(row, "decision")
                        : BatchJsonString(row, "error"),
                    Attempts = JsonInt(row, "attempts")
                });
            }
        }

        private static string FormatPaymentBatchSummary(JsonElement report)
        {
            if (!report.TryGetProperty("counts", out JsonElement counts) || counts.ValueKind != JsonValueKind.Object)
                return "批次已结束，但未返回计数。";
            int requested = JsonInt(counts, "requested");
            int authenticated = JsonInt(counts, "authenticated");
            int refreshed = JsonInt(counts, "refreshed");
            int eligible = JsonInt(counts, "eligible");
            int completed = JsonInt(counts, "completed");
            int links = JsonInt(counts, "link_ready");
            int qr = JsonInt(counts, "qr_ready");
            int failed = JsonInt(counts, "failed");
            int resumed = JsonInt(report, "resumed");
            return $"请求 {requested}  ·  AT 200 {authenticated}  ·  JIT {refreshed}  ·  资格 {eligible}  ·  完成 {completed}  ·  链接 {links}  ·  二维码 {qr}  ·  失败 {failed}  ·  断点恢复 {resumed}";
        }

        private static string BatchJsonString(JsonElement element, string name)
        {
            if (!element.TryGetProperty(name, out JsonElement value)) return "";
            return value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : value.ToString();
        }

        private static int JsonInt(JsonElement element, string name)
        {
            if (!element.TryGetProperty(name, out JsonElement value)) return 0;
            if (value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out int number)) return number;
            return int.TryParse(value.ToString(), out number) ? number : 0;
        }

        private static bool JsonBool(JsonElement element, string name)
        {
            return element.TryGetProperty(name, out JsonElement value) && value.ValueKind == JsonValueKind.True;
        }

        private static void TryDeleteTemporaryFile(string path)
        {
            try { if (!string.IsNullOrWhiteSpace(path) && File.Exists(path)) File.Delete(path); }
            catch { }
        }

        private sealed class PaymentMatrixRow
        {
            public string Name { get; set; } = "default";
            public string RegistrationCountry { get; set; } = "";
            public string CheckoutCountry { get; set; } = "";
            public string PromotionCountry { get; set; } = "";
            public string ProviderCountry { get; set; } = "";
            public string ApproveCountry { get; set; } = "";
            public string RedirectCountry { get; set; } = "";
            public string Strategy { get; set; } = "";
            public int SampleSize { get; set; } = 1;

            public bool IsValid()
            {
                bool Country(string value) => string.IsNullOrWhiteSpace(value) || Regex.IsMatch(value.Trim(), "^[A-Za-z]{2}$");
                return !string.IsNullOrWhiteSpace(Name) && SampleSize > 0
                    && Country(RegistrationCountry) && Country(CheckoutCountry) && Country(PromotionCountry)
                    && Country(ProviderCountry) && Country(ApproveCountry) && Country(RedirectCountry);
            }
        }

        private sealed class PaymentBatchResultRow
        {
            public string AccountRef { get; set; } = "";
            public string MatrixCell { get; set; } = "";
            public string AuthStatus { get; set; } = "";
            public string RefreshStatus { get; set; } = "";
            public string Eligibility { get; set; } = "";
            public string Decision { get; set; } = "";
            public int Attempts { get; set; }
        }
    }
}
