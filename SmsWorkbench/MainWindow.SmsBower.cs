using System.Text.Json.Nodes;

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        private async Task<bool> ShowSmsBowerOneClickDialogAsync(CancellationToken ct = default)
        {
            string apiKey = ResolveSmsBowerApiKey(settingsService.GetString("phone_reuse.smsbower.api_key"));
            if (string.IsNullOrWhiteSpace(apiKey))
            {
                ShowThemedInfoDialog("SMSBower 未配置", "请先在设置的手机接码分类中填写 SMSBower API Key。");
                return false;
            }

            string endpoint = FirstNonEmpty(settingsService.GetString("phone_reuse.smsbower.endpoint"), SmsBowerCatalogClient.DefaultEndpoint);
            IReadOnlyList<SmsBowerCountryChoice> countries;
            string balance = "--";
            try
            {
                System.Windows.Input.Mouse.OverrideCursor = System.Windows.Input.Cursors.Wait;
                countries = await SmsBowerCatalogClient.LoadOpenAiCatalogAsync(httpClient, apiKey, endpoint);
                try
                {
                    balance = await SmsBowerCatalogClient.LoadBalanceAsync(httpClient, apiKey, endpoint);
                }
                catch (Exception balanceError)
                {
                    logger?.Warning(balanceError, "Failed to load SMSBower balance");
                }
            }
            catch (Exception exc)
            {
                logger?.Error(exc, "Failed to load SMSBower OpenAI catalog");
                ShowThemedInfoDialog("SMSBower 加载失败", "无法读取 OpenAI 号码地区和价格档位：" + exc.Message);
                return false;
            }
            finally
            {
                System.Windows.Input.Mouse.OverrideCursor = null;
            }

            if (countries.Count == 0)
            {
                ShowThemedInfoDialog("暂无号码", "SMSBower 当前没有可用的 OpenAI 号码。");
                return false;
            }

            string savedCountry = FirstNonEmpty(settingsService.GetString("phone_reuse.smsbower.country"), "38");
            string savedPrice = FirstNonEmpty(
                settingsService.GetString("phone_reuse.smsbower.target_price"),
                settingsService.GetString("phone_reuse.smsbower.max_price"),
                settingsService.GetString("phone_reuse.smsbower.min_price"));
            var selectedCountry = countries.FirstOrDefault(item => item.Id == savedCountry) ?? countries[0];
            var selectedTier = selectedCountry.Tiers.FirstOrDefault(item => PriceEquals(item.Price, savedPrice))
                ?? selectedCountry.Tiers[0];

            var dialog = new Window
            {
                Title = "一键接码",
                Owner = this,
                Width = Math.Min(620, SystemParameters.WorkArea.Width - 60),
                Height = 420,
                MinWidth = 520,
                MinHeight = 380,
                ResizeMode = ResizeMode.CanResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(24) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var headingPanel = new StackPanel
            {
                Margin = new Thickness(0, 0, 0, 18)
            };
            var heading = new TextBlock
            {
                Text = "选择 SMSBower 号码",
                FontSize = 20,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 4)
            };
            var balanceText = new TextBlock
            {
                Text = "当前平台余额：$" + balance,
                FontSize = 13,
                Foreground = (Brush)FindResource("TextSub")
            };
            headingPanel.Children.Add(heading);
            headingPanel.Children.Add(balanceText);
            Grid.SetRow(headingPanel, 0);
            root.Children.Add(headingPanel);

            var servicePanel = CreateSmsBowerDialogRow("服务商", out ContentControl serviceHost);
            serviceHost.Content = new TextBlock
            {
                Text = "OpenAI (ChatGPT)",
                FontSize = 14,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain"),
                VerticalAlignment = VerticalAlignment.Center
            };
            Grid.SetRow(servicePanel, 1);
            root.Children.Add(servicePanel);

            var countryPanel = CreateSmsBowerDialogRow("国家或地区", out ContentControl countryHost);
            var countryBox = new ComboBox
            {
                ItemsSource = countries,
                DisplayMemberPath = nameof(SmsBowerCountryChoice.DisplayName),
                SelectedItem = selectedCountry,
                IsTextSearchEnabled = true,
                MaxDropDownHeight = 280,
                MinHeight = 36,
                Padding = new Thickness(8, 4, 8, 4)
            };
            countryHost.Content = countryBox;
            Grid.SetRow(countryPanel, 2);
            root.Children.Add(countryPanel);

            var tierPanel = CreateSmsBowerDialogRow("号码档位", out ContentControl tierHost);
            var tierBox = new ComboBox
            {
                ItemsSource = selectedCountry.Tiers,
                DisplayMemberPath = nameof(SmsBowerPriceTier.DisplayName),
                SelectedItem = selectedTier,
                MaxDropDownHeight = 260,
                MinHeight = 36,
                Padding = new Thickness(8, 4, 8, 4)
            };
            tierHost.Content = tierBox;
            Grid.SetRow(tierPanel, 3);
            root.Children.Add(tierPanel);

            var inventory = new TextBlock
            {
                Text = "",
                Foreground = (Brush)FindResource("TextMuted"),
                FontSize = 12,
                Margin = new Thickness(142, 8, 0, 0)
            };
            Grid.SetRow(inventory, 4);
            root.Children.Add(inventory);

            void RefreshInventory()
            {
                if (tierBox.SelectedItem is SmsBowerPriceTier tier)
                {
                    inventory.Text = $"当前库存 {tier.Count} 个，价格 ${tier.Price} / 个";
                }
                else
                {
                    inventory.Text = "";
                }
            }

            countryBox.SelectionChanged += (_, _) =>
            {
                if (countryBox.SelectedItem is not SmsBowerCountryChoice country) return;
                tierBox.ItemsSource = country.Tiers;
                tierBox.SelectedItem = country.Tiers[0];
                RefreshInventory();
            };
            tierBox.SelectionChanged += (_, _) => RefreshInventory();
            RefreshInventory();

            var buttons = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 20, 0, 0)
            };
            var cancel = new Button
            {
                Content = "取消",
                MinWidth = 88,
                Height = 36,
                Margin = new Thickness(0, 0, 10, 0),
                IsCancel = true
            };
            var start = new Button
            {
                Content = "开始接码",
                MinWidth = 104,
                Height = 36,
                IsDefault = true
            };
            start.Click += (_, _) => dialog.DialogResult = true;
            buttons.Children.Add(cancel);
            buttons.Children.Add(start);
            Grid.SetRow(buttons, 5);
            root.Children.Add(buttons);

            dialog.Content = root;
            if (dialog.ShowDialog() != true
                || countryBox.SelectedItem is not SmsBowerCountryChoice chosenCountry
                || tierBox.SelectedItem is not SmsBowerPriceTier chosenTier)
            {
                return false;
            }

            settingsService.UpdateConfig(root =>
            {
                JsonObject smsBower = GetOrCreateSection(GetOrCreateSection(root, "phone_reuse"), "smsbower");
                smsBower["service"] = SmsBowerCatalogClient.OpenAiService;
                smsBower["service_name"] = "OpenAI (ChatGPT)";
                smsBower["country"] = chosenCountry.Id;
                smsBower["country_name"] = chosenCountry.EnglishName;
                smsBower["country_name_zh"] = chosenCountry.ChineseName;
                smsBower.Remove("country_prefix");
                smsBower["min_price"] = chosenTier.Price;
                smsBower["max_price"] = chosenTier.Price;
                smsBower["target_price"] = chosenTier.Price;
                if (string.IsNullOrWhiteSpace(chosenTier.ProviderIds))
                {
                    smsBower.Remove("provider_ids");
                }
                else
                {
                    smsBower["provider_ids"] = chosenTier.ProviderIds;
                }
            });
            return true;
        }

        private static JsonObject GetOrCreateSection(JsonObject parent, string key)
        {
            if (parent[key] is not JsonObject child)
            {
                child = new JsonObject();
                parent[key] = child;
            }
            return child;
        }

        private Grid CreateSmsBowerDialogRow(string label, out ContentControl host)
        {
            var row = new Grid { Margin = new Thickness(0, 0, 0, 14) };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(126) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.Children.Add(new TextBlock
            {
                Text = label,
                FontSize = 13,
                Foreground = (Brush)FindResource("TextSub"),
                VerticalAlignment = VerticalAlignment.Center
            });
            host = new ContentControl { VerticalContentAlignment = VerticalAlignment.Center };
            Grid.SetColumn(host, 1);
            row.Children.Add(host);
            return row;
        }

        private static string ResolveSmsBowerApiKey(string configured)
        {
            string value = (configured ?? "").Trim();
            if (value.Length == 0 || value == "$SMSBOWER_API_KEY" || value == "YOUR_SMSBOWER_API_KEY")
            {
                return (Environment.GetEnvironmentVariable("SMSBOWER_API_KEY") ?? "").Trim();
            }
            return value;
        }

        private static bool PriceEquals(string left, string right)
        {
            return decimal.TryParse(left, NumberStyles.Number, CultureInfo.InvariantCulture, out decimal a)
                && decimal.TryParse(right, NumberStyles.Number, CultureInfo.InvariantCulture, out decimal b)
                && a == b;
        }

    }
}
