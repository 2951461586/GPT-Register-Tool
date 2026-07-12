using System.Windows.Input;

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        private sealed class GmailAliasListItem
        {
            public string BaseEmail { get; set; } = "";
            public int AliasCount { get; set; }

            public override string ToString()
            {
                return AliasCount > 0 ? $"{BaseEmail} ({AliasCount})" : BaseEmail;
            }
        }

        private void EnsureAliasColumnInteractions()
        {
            if (AccountGrid == null || aliasColumnInteractionsHooked) return;
            AccountGrid.MouseDoubleClick += AccountGrid_MouseDoubleClick;
            aliasColumnInteractionsHooked = true;
        }

        private void AccountGrid_MouseDoubleClick(object sender, MouseButtonEventArgs e)
        {
            if (e.OriginalSource is not DependencyObject source) return;
            DataGridCell cell = FindVisualParent<DataGridCell>(source);
            if (cell == null) return;
            string header = Convert.ToString(cell.Column?.Header) ?? "";
            if (!header.Equals("Alias", StringComparison.OrdinalIgnoreCase)) return;

            PoolRow row = cell.DataContext as PoolRow ?? SelectedRow ?? (AccountGrid?.SelectedItem as PoolRow);
            if (row == null || string.IsNullOrWhiteSpace(row.AliasNotes)) return;

            ShowAliasNotesDialog(row);
            e.Handled = true;
        }

        private static T FindVisualParent<T>(DependencyObject node) where T : DependencyObject
        {
            while (node != null)
            {
                if (node is T typed) return typed;
                node = VisualTreeHelper.GetParent(node);
            }
            return null;
        }

        private string ResolveAliasBaseEmail(string email)
        {
            string canonical = CanonicalGmailEmail(email);
            if (canonical.Length == 0) return "";
            if (gmailAliasBases.TryGetValue(canonical, out string baseEmail) && !string.IsNullOrWhiteSpace(baseEmail))
            {
                return baseEmail.Trim();
            }
            return canonical;
        }

        private bool TryGetGmailAliasBundle(string email, out string baseEmail, out List<string> aliases, out bool isBaseMailbox)
        {
            aliases = new List<string>();
            baseEmail = ResolveAliasBaseEmail(email);
            isBaseMailbox = false;

            string canonical = CanonicalGmailEmail(email);
            if (canonical.Length == 0 || baseEmail.Length == 0) return false;

            if (gmailAliasMap.TryGetValue(canonical, out List<string> storedAliases) && storedAliases != null)
            {
                aliases = storedAliases
                    .Where(item => !string.IsNullOrWhiteSpace(item))
                    .Select(item => item.Trim())
                    .Distinct(StringComparer.OrdinalIgnoreCase)
                    .ToList();
            }

            isBaseMailbox = NormalizeEmailKey(email).Equals(NormalizeEmailKey(baseEmail), StringComparison.OrdinalIgnoreCase);
            return true;
        }

        private string DescribeGmailAliasOwnership(string email)
        {
            if (!TryGetGmailAliasBundle(email, out string baseEmail, out List<string> aliases, out bool isBaseMailbox))
            {
                return "";
            }

            string ownerPrefix = isBaseMailbox ? "Gmail 主邮箱" : "Alias 归属 Gmail 主邮箱";
            if (aliases.Count > 0)
            {
                return $"{ownerPrefix}: {baseEmail}  |  Alias 数: {aliases.Count}";
            }
            return $"{ownerPrefix}: {baseEmail}";
        }

        private void ShowAliasNotesDialog(PoolRow row)
        {
            if (row == null) return;
            if (!TryGetGmailAliasBundle(row.Identifier, out string baseEmail, out List<string> aliases, out bool isBaseMailbox))
            {
                return;
            }

            var dialog = new Window
            {
                Title = "Alias 详情 - " + row.Identifier,
                Owner = this,
                Width = 760,
                Height = 520,
                MinWidth = 620,
                MinHeight = 420,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(16) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var header = new StackPanel { Margin = new Thickness(0, 0, 0, 12) };
            header.Children.Add(new TextBlock
            {
                Text = row.Identifier,
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain")
            });
            header.Children.Add(new TextBlock
            {
                Text = isBaseMailbox ? $"Gmail 主邮箱: {baseEmail}" : $"Alias 归属 Gmail 主邮箱: {baseEmail}",
                FontSize = 12,
                Foreground = (Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 6, 0, 0)
            });
            header.Children.Add(new TextBlock
            {
                Text = $"Alias 数量: {aliases.Count}",
                FontSize = 12,
                Foreground = (Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 2, 0, 0)
            });
            Grid.SetRow(header, 0);
            root.Children.Add(header);

            var aliasBox = new TextBox
            {
                Text = aliases.Count > 0 ? string.Join(Environment.NewLine, aliases) : row.AliasNotes ?? "",
                IsReadOnly = true,
                AcceptsReturn = true,
                TextWrapping = TextWrapping.NoWrap,
                FontFamily = new FontFamily("Consolas"),
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                Padding = new Thickness(10, 8, 10, 8),
                Background = (Brush)FindResource("PanelBg"),
                Foreground = (Brush)FindResource("TextMain"),
                BorderBrush = (Brush)FindResource("Line")
            };
            Grid.SetRow(aliasBox, 1);
            root.Children.Add(aliasBox);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 12, 0, 0)
            };
            var copyButton = new Button
            {
                Content = "复制",
                Width = 80,
                Margin = new Thickness(0, 0, 8, 0)
            };
            copyButton.Click += (_, __) =>
            {
                Clipboard.SetText(aliasBox.Text ?? "");
                Log("Alias 内容已复制。");
            };
            var closeButton = new Button
            {
                Content = "关闭",
                Width = 80,
                Style = (Style)FindResource("PrimaryButton")
            };
            closeButton.Click += (_, __) => dialog.Close();
            actions.Children.Add(copyButton);
            actions.Children.Add(closeButton);
            Grid.SetRow(actions, 2);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private void ShowGmailAliasManagerDialog()
        {
            LoadGmailAliasNotes();
            string path = GetGmailAliasFilePath();
            Directory.CreateDirectory(Path.GetDirectoryName(path) ?? Path.Combine(rootDir, "runtime"));

            Dictionary<string, List<string>> store = LoadGmailAliasStore();
            foreach (string knownBase in GetKnownGmailBaseEmails())
            {
                if (!store.ContainsKey(knownBase))
                {
                    store[knownBase] = new List<string>();
                }
            }

            var dialog = new Window
            {
                Title = "Gmail Alias 管理",
                Owner = this,
                Width = 980,
                Height = 640,
                MinWidth = 820,
                MinHeight = 520,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var items = new ObservableCollection<GmailAliasListItem>();
            var root = new Grid { Margin = new Thickness(16) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var intro = new TextBlock
            {
                Text = "在这里直接维护 Gmail 主邮箱与 alias 的映射。每行一个 alias，保存后邮箱池列表会自动刷新。",
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 12)
            };
            Grid.SetRow(intro, 0);
            root.Children.Add(intro);

            var content = new Grid();
            content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(260) });
            content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(16) });
            content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            Grid.SetRow(content, 1);
            root.Children.Add(content);

            var leftPanel = new Grid();
            leftPanel.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            leftPanel.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            leftPanel.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            Grid.SetColumn(leftPanel, 0);
            content.Children.Add(leftPanel);

            leftPanel.Children.Add(new TextBlock
            {
                Text = "Gmail 主邮箱",
                FontSize = 14,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 10)
            });

            var baseList = new ListBox
            {
                Margin = new Thickness(0, 0, 0, 10),
                BorderBrush = (Brush)FindResource("Line"),
                Background = (Brush)FindResource("PanelBg"),
                Foreground = (Brush)FindResource("TextMain")
            };
            Grid.SetRow(baseList, 1);
            leftPanel.Children.Add(baseList);

            var leftActions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Stretch
            };
            var addButton = new Button { Content = "新建", Width = 72 };
            var reloadButton = new Button { Content = "重载", Width = 72, Margin = new Thickness(8, 0, 0, 0) };
            leftActions.Children.Add(addButton);
            leftActions.Children.Add(reloadButton);
            Grid.SetRow(leftActions, 2);
            leftPanel.Children.Add(leftActions);

            var editor = new Grid();
            editor.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            editor.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            editor.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            Grid.SetColumn(editor, 2);
            content.Children.Add(editor);

            var baseEmailLabel = new TextBlock
            {
                Text = "主邮箱",
                Foreground = (Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 6)
            };
            Grid.SetRow(baseEmailLabel, 0);
            editor.Children.Add(baseEmailLabel);

            var baseEmailBox = new TextBox
            {
                MinHeight = 36,
                Padding = new Thickness(8, 5, 8, 5),
                Margin = new Thickness(0, 0, 0, 12)
            };
            Grid.SetRow(baseEmailBox, 1);
            editor.Children.Add(baseEmailBox);

            var aliasPanel = new Grid();
            aliasPanel.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            aliasPanel.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            aliasPanel.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            Grid.SetRow(aliasPanel, 2);
            editor.Children.Add(aliasPanel);

            aliasPanel.Children.Add(new TextBlock
            {
                Text = "Alias 列表（每行一个）",
                Foreground = (Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 6)
            });

            var helperText = new TextBlock
            {
                Text = "支持大小写、点号和 +tag 的原始写法；保存后仍按 Gmail canonical 规则匹配。",
                Foreground = (Brush)FindResource("TextMuted"),
                Margin = new Thickness(0, 22, 0, 10),
                TextWrapping = TextWrapping.Wrap
            };
            Grid.SetRow(helperText, 1);
            aliasPanel.Children.Add(helperText);

            var aliasBox = new TextBox
            {
                AcceptsReturn = true,
                TextWrapping = TextWrapping.NoWrap,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                FontFamily = new FontFamily("Consolas"),
                Padding = new Thickness(8),
                Background = (Brush)FindResource("PanelBg"),
                BorderBrush = (Brush)FindResource("Line")
            };
            Grid.SetRow(aliasBox, 2);
            aliasPanel.Children.Add(aliasBox);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 12, 0, 0)
            };
            var openJsonButton = new Button { Content = "打开 JSON", Width = 108 };
            var deleteButton = new Button { Content = "删除当前", Width = 88, Margin = new Thickness(8, 0, 0, 0) };
            var saveButton = new Button { Content = "保存", Width = 80, Margin = new Thickness(8, 0, 0, 0), Style = (Style)FindResource("PrimaryButton") };
            var closeButton = new Button { Content = "关闭", Width = 80, Margin = new Thickness(8, 0, 0, 0) };
            actions.Children.Add(openJsonButton);
            actions.Children.Add(deleteButton);
            actions.Children.Add(saveButton);
            actions.Children.Add(closeButton);
            Grid.SetRow(actions, 2);
            root.Children.Add(actions);

            void RefreshItems(string preferredBase = "")
            {
                items.Clear();
                foreach (string baseEmail in store.Keys.OrderBy(value => value, StringComparer.OrdinalIgnoreCase))
                {
                    items.Add(new GmailAliasListItem
                    {
                        BaseEmail = baseEmail,
                        AliasCount = store.TryGetValue(baseEmail, out List<string> aliases) && aliases != null ? aliases.Count : 0
                    });
                }
                baseList.ItemsSource = items;

                string target = (preferredBase ?? "").Trim();
                if (target.Length == 0 && items.Count > 0)
                {
                    target = items[0].BaseEmail;
                }
                if (target.Length == 0)
                {
                    baseList.SelectedItem = null;
                    return;
                }

                GmailAliasListItem selected = items.FirstOrDefault(item => item.BaseEmail.Equals(target, StringComparison.OrdinalIgnoreCase));
                if (selected != null)
                {
                    baseList.SelectedItem = selected;
                    baseList.ScrollIntoView(selected);
                }
            }

            void LoadEditor(string baseEmail)
            {
                string selectedBase = (baseEmail ?? "").Trim();
                baseEmailBox.Text = selectedBase;
                aliasBox.Text = store.TryGetValue(selectedBase, out List<string> aliases) && aliases != null
                    ? string.Join(Environment.NewLine, aliases)
                    : "";
            }

            bool SaveCurrent(out string savedBaseEmail)
            {
                savedBaseEmail = "";
                string baseEmail = NormalizeEmailKey(baseEmailBox.Text);
                if (CanonicalGmailEmail(baseEmail).Length == 0)
                {
                    MessageBox.Show("请输入有效的 Gmail 主邮箱。", "格式不正确", MessageBoxButton.OK, MessageBoxImage.Information);
                    return false;
                }

                var aliases = ParseAliasLines(aliasBox.Text)
                    .Where(item => CanonicalGmailEmail(item).Length > 0)
                    .Where(item => !NormalizeEmailKey(item).Equals(baseEmail, StringComparison.OrdinalIgnoreCase))
                    .Distinct(StringComparer.OrdinalIgnoreCase)
                    .ToList();

                store[baseEmail] = aliases;
                SaveGmailAliasStore(store);
                LoadGmailAliasNotes();
                RefreshPools();
                savedBaseEmail = baseEmail;
                Log($"Gmail alias 已保存：{baseEmail} ({aliases.Count})");
                return true;
            }

            baseList.SelectionChanged += (_, __) =>
            {
                if (baseList.SelectedItem is GmailAliasListItem selected)
                {
                    LoadEditor(selected.BaseEmail);
                }
            };
            baseList.MouseDoubleClick += (_, __) =>
            {
                if (baseList.SelectedItem is GmailAliasListItem selected)
                {
                    LoadEditor(selected.BaseEmail);
                    aliasBox.Focus();
                }
            };
            addButton.Click += (_, __) =>
            {
                baseList.SelectedItem = null;
                baseEmailBox.Text = "";
                aliasBox.Text = "";
                baseEmailBox.Focus();
            };
            reloadButton.Click += (_, __) =>
            {
                store = LoadGmailAliasStore();
                foreach (string knownBase in GetKnownGmailBaseEmails())
                {
                    if (!store.ContainsKey(knownBase))
                    {
                        store[knownBase] = new List<string>();
                    }
                }
                LoadGmailAliasNotes();
                RefreshItems(baseEmailBox.Text);
                if (baseList.SelectedItem is GmailAliasListItem selected)
                {
                    LoadEditor(selected.BaseEmail);
                }
                else if (!string.IsNullOrWhiteSpace(baseEmailBox.Text))
                {
                    aliasBox.Text = store.TryGetValue(NormalizeEmailKey(baseEmailBox.Text), out List<string> aliases)
                        ? string.Join(Environment.NewLine, aliases)
                        : "";
                }
                Log("Gmail alias 映射已从磁盘重载。");
            };
            openJsonButton.Click += (_, __) => OpenPath(path);
            saveButton.Click += (_, __) =>
            {
                if (!SaveCurrent(out string savedBaseEmail)) return;
                if (!store.ContainsKey(savedBaseEmail))
                {
                    store[savedBaseEmail] = new List<string>();
                }
                RefreshItems(savedBaseEmail);
                LoadEditor(savedBaseEmail);
            };
            deleteButton.Click += (_, __) =>
            {
                string baseEmail = NormalizeEmailKey(baseEmailBox.Text);
                if (baseEmail.Length == 0 || !store.ContainsKey(baseEmail))
                {
                    MessageBox.Show("当前主邮箱还没有可删除的 alias 记录。", "删除 Gmail Alias", MessageBoxButton.OK, MessageBoxImage.Information);
                    return;
                }
                if (MessageBox.Show($"确定删除 {baseEmail} 的 alias 映射吗？", "删除 Gmail Alias", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes)
                {
                    return;
                }

                store.Remove(baseEmail);
                SaveGmailAliasStore(store);
                LoadGmailAliasNotes();
                RefreshPools();
                RefreshItems();
                if (baseList.SelectedItem is GmailAliasListItem selected)
                {
                    LoadEditor(selected.BaseEmail);
                }
                else
                {
                    baseEmailBox.Text = "";
                    aliasBox.Text = "";
                }
                Log($"Gmail alias 已删除：{baseEmail}");
            };
            closeButton.Click += (_, __) => dialog.Close();

            RefreshItems();
            if (baseList.SelectedItem is GmailAliasListItem initial)
            {
                LoadEditor(initial.BaseEmail);
            }

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private Dictionary<string, List<string>> LoadGmailAliasStore()
        {
            var result = new Dictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);
            string path = GetGmailAliasFilePath();
            if (!File.Exists(path)) return result;

            try
            {
                Dictionary<string, object> data = ReadJsonObject(path);
                foreach (var kv in data)
                {
                    string baseEmail = NormalizeEmailKey(kv.Key);
                    if (CanonicalGmailEmail(baseEmail).Length == 0) continue;

                    var aliases = new List<string>();
                    if (kv.Value is List<object> items)
                    {
                        foreach (object item in items)
                        {
                            string alias = Convert.ToString(item)?.Trim() ?? "";
                            if (CanonicalGmailEmail(alias).Length == 0) continue;
                            if (!aliases.Contains(alias, StringComparer.OrdinalIgnoreCase))
                            {
                                aliases.Add(alias);
                            }
                        }
                    }
                    result[baseEmail] = aliases;
                }
            }
            catch (Exception ex)
            {
                Log("Gmail alias load failed: " + ex.Message);
            }
            return result;
        }

        private void SaveGmailAliasStore(Dictionary<string, List<string>> store)
        {
            string path = GetGmailAliasFilePath();
            Directory.CreateDirectory(Path.GetDirectoryName(path) ?? Path.Combine(rootDir, "runtime"));

            var payload = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            foreach (string baseEmail in store.Keys.OrderBy(value => value, StringComparer.OrdinalIgnoreCase))
            {
                var aliases = (store[baseEmail] ?? new List<string>())
                    .Where(item => !string.IsNullOrWhiteSpace(item))
                    .Select(item => item.Trim())
                    .Distinct(StringComparer.OrdinalIgnoreCase)
                    .Cast<object>()
                    .ToList();
                payload[baseEmail] = aliases;
            }

            var options = new JsonSerializerOptions { WriteIndented = true };
            File.WriteAllText(path, JsonSerializer.Serialize(payload, options), new UTF8Encoding(false));
        }

        private List<string> ParseAliasLines(string raw)
        {
            return (raw ?? "")
                .Split(new[] { "\r\n", "\n", "," }, StringSplitOptions.RemoveEmptyEntries)
                .Select(item => item.Trim())
                .Where(item => item.Length > 0)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToList();
        }

        private List<string> GetKnownGmailBaseEmails()
        {
            var items = new List<string>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

            void AddCandidate(string email)
            {
                string normalized = NormalizeEmailKey(ResolveAliasBaseEmail(email));
                if (normalized.Length == 0)
                {
                    normalized = NormalizeEmailKey(email);
                }
                if (CanonicalGmailEmail(normalized).Length == 0) return;
                if (seen.Add(normalized))
                {
                    items.Add(normalized);
                }
            }

            foreach (string baseEmail in gmailAliasBases.Values)
            {
                AddCandidate(baseEmail);
            }

            foreach (PoolRow row in allRows)
            {
                if (row == null) continue;
                if (CanonicalGmailEmail(row.Identifier).Length == 0
                    && !row.RawLine.StartsWith("gmail://", StringComparison.OrdinalIgnoreCase)
                    && !row.MailboxProvider.Equals("gmail", StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }
                AddCandidate(row.Identifier);
            }

            return items.OrderBy(item => item, StringComparer.OrdinalIgnoreCase).ToList();
        }
    }
}
