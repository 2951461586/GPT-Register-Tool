// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System.Text.Json.Nodes;

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        /// <summary>
        /// One-click SMS rental dialog.
        ///
        /// Every supported provider speaks the same sms-activate handler
        /// protocol, so the dialog is provider-agnostic: it reads whatever
        /// `phone_reuse.source` selects. Exactly three things are
        /// provider-specific, and all three come from
        /// <see cref="SmsProviderCatalog"/> -- the config section
        /// (`phone_reuse.&lt;provider&gt;`), the default endpoint, and the API-key
        /// environment variable.
        /// </summary>
        private async Task<bool> ShowSmsProviderOneClickDialogAsync(CancellationToken ct = default)
        {
            SmsProviderCatalog.SmsProvider provider =
                SmsProviderCatalog.Resolve(settingsService.GetString("phone_reuse.source"));
            string section = "phone_reuse." + provider.Key;
            string apiKey = ResolveSmsProviderApiKey(settingsService.GetString(section + ".api_key"), provider);
            if (string.IsNullOrWhiteSpace(apiKey))
            {
                ShowThemedInfoDialog(
                    provider.Label + " 未配置",
                    $"请先在设置的「接码供应商」分类中填写 {provider.Label} 的 API Key"
                    + $"（配置键 {section}.api_key，或环境变量 {provider.ApiKeyEnv}）。");
                return false;
            }

            string endpoint = FirstNonEmpty(settingsService.GetString(section + ".endpoint"), provider.DefaultEndpoint);
            IReadOnlyList<SmsProviderCountryChoice> countries;
            string balance = "--";
            try
            {
                System.Windows.Input.Mouse.OverrideCursor = System.Windows.Input.Cursors.Wait;
                countries = await SmsProviderCatalogClient.LoadOpenAiCatalogAsync(httpClient, apiKey, endpoint);
                try
                {
                    balance = await SmsProviderCatalogClient.LoadBalanceAsync(httpClient, apiKey, endpoint);
                }
                catch (Exception balanceError)
                {
                    logger?.Warning(balanceError, "Failed to load {Provider} balance", provider.Label);
                }
            }
            catch (Exception exc)
            {
                logger?.Error(exc, "Failed to load {Provider} OpenAI catalog", provider.Label);
                ShowThemedInfoDialog(provider.Label + " 加载失败", "无法读取 OpenAI 号码地区和价格档位：" + exc.Message);
                return false;
            }
            finally
            {
                System.Windows.Input.Mouse.OverrideCursor = null;
            }

            if (countries.Count == 0)
            {
                ShowThemedInfoDialog("暂无号码", provider.Label + " 当前没有可用的 OpenAI 号码。");
                return false;
            }

            string savedCountry = FirstNonEmpty(settingsService.GetString(section + ".country"), "38");
            string savedPrice = FirstNonEmpty(
                settingsService.GetString(section + ".target_price"),
                settingsService.GetString(section + ".max_price"),
                settingsService.GetString(section + ".min_price"));
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
                Text = "选择 " + provider.Label + " 号码",
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

            var servicePanel = CreateSmsProviderDialogRow("服务商", out ContentControl serviceHost);
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

            var countryPanel = CreateSmsProviderDialogRow("国家或地区", out ContentControl countryHost);
            var countryBox = new ComboBox
            {
                ItemsSource = countries,
                DisplayMemberPath = nameof(SmsProviderCountryChoice.DisplayName),
                SelectedItem = selectedCountry,
                IsTextSearchEnabled = true,
                MaxDropDownHeight = 280,
                MinHeight = 36,
                Padding = new Thickness(8, 4, 8, 4)
            };
            countryHost.Content = countryBox;
            Grid.SetRow(countryPanel, 2);
            root.Children.Add(countryPanel);

            var tierPanel = CreateSmsProviderDialogRow("号码档位", out ContentControl tierHost);
            var tierBox = new ComboBox
            {
                ItemsSource = selectedCountry.Tiers,
                DisplayMemberPath = nameof(SmsProviderPriceTier.DisplayName),
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
                if (tierBox.SelectedItem is SmsProviderPriceTier tier)
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
                if (countryBox.SelectedItem is not SmsProviderCountryChoice country) return;
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
                || countryBox.SelectedItem is not SmsProviderCountryChoice chosenCountry
                || tierBox.SelectedItem is not SmsProviderPriceTier chosenTier)
            {
                return false;
            }

            settingsService.UpdateConfig(root =>
            {
                JsonObject providerSection = GetOrCreateSection(GetOrCreateSection(root, "phone_reuse"), provider.Key);
                providerSection["service"] = SmsProviderCatalogClient.OpenAiService;
                providerSection["service_name"] = "OpenAI (ChatGPT)";
                providerSection["country"] = chosenCountry.Id;
                providerSection["country_name"] = chosenCountry.EnglishName;
                providerSection["country_name_zh"] = chosenCountry.ChineseName;
                providerSection.Remove("country_prefix");
                providerSection["min_price"] = chosenTier.Price;
                providerSection["max_price"] = chosenTier.Price;
                providerSection["target_price"] = chosenTier.Price;
                if (string.IsNullOrWhiteSpace(chosenTier.ProviderIds))
                {
                    providerSection.Remove("provider_ids");
                }
                else
                {
                    providerSection["provider_ids"] = chosenTier.ProviderIds;
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

        private Grid CreateSmsProviderDialogRow(string label, out ContentControl host)
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

        /// <summary>
        /// Resolve the configured key, falling back to the provider's own
        /// environment variable.
        ///
        /// The two placeholder shapes mirror `phone_reuse._resolve_secret` on the
        /// Python side, so a value the desktop accepts is a value the backend
        /// also accepts -- and an operator who writes `$HEROSMS_API_KEY` into
        /// the config gets the same behaviour from both ends.
        /// </summary>
        private static string ResolveSmsProviderApiKey(string configured, SmsProviderCatalog.SmsProvider provider)
        {
            string value = (configured ?? "").Trim();
            if (value.Length == 0
                || value == "$" + provider.ApiKeyEnv
                || value == "YOUR_" + provider.ApiKeyEnv)
            {
                return (Environment.GetEnvironmentVariable(provider.ApiKeyEnv) ?? "").Trim();
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
