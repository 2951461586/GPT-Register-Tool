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
        /// Four things are provider-specific and all four come from
        /// <see cref="SmsProviderCatalog"/> -- the config section
        /// (`phone_reuse.&lt;provider&gt;`), the default endpoint, the API-key
        /// environment variable, and the **protocol family**.
        ///
        /// <para>
        /// 🔴 The dialog is *not* provider-agnostic. It used to claim it was, on
        /// the grounds that every provider speaks the sms-activate handler
        /// protocol; that stopped being true when `nexsms` was wired up, and the
        /// claim was load-bearing -- the online country/price lookup below
        /// (`?action=getCountries` / `getPricesV3` / an `ACCESS_BALANCE:` reply)
        /// is sms-activate-only. nexsms answers all three with `403 Forbidden`,
        /// so selecting it and clicking 一键接码 used to dead-end in
        /// "加载失败 … 403 (Forbidden)" with no way forward.
        /// </para>
        ///
        /// <para>
        /// When the provider is not sms-activate the dialog therefore skips the
        /// network entirely and takes the country and price tier **from the
        /// config**, which is what the backend reads anyway. It does not write
        /// them back: the values it shows *are* the config values, so a write
        /// could only reformat them -- and it would add `service_name` /
        /// `country_name_zh`, two write-only leaves whose appearance in a
        /// non-`smsbower` section turns `tests/test_config_usage.py` red
        /// (see `docs/TROUBLESHOOTING.md` §13).
        /// </para>
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

            IReadOnlyList<SmsProviderCountryChoice> countries;
            string balance = "--";
            string notice = "";
            if (provider.CatalogIsSmsActivate)
            {
                string endpoint = FirstNonEmpty(settingsService.GetString(section + ".endpoint"), provider.DefaultEndpoint);
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
            }
            else
            {
                // Not sms-activate: read the country and tier from the config
                // instead of the wire. Reimplementing this vendor's REST catalog
                // here would be a second copy of a protocol `sms_tool` already
                // implements, and the copy is the one that would rot.
                SmsProviderCountryChoice? saved = ReadSavedSmsProviderChoice(section);
                if (saved is null)
                {
                    ShowThemedInfoDialog(
                        provider.Label + " 缺少国家或档位",
                        $"请先在 {section} 里配置 country 与 target_price（或 max_price / min_price），"
                        + $"或改用命令行查询 {provider.Label} 的可用国家与价格。");
                    return false;
                }

                countries = new[] { saved };
                notice = $"{provider.Label} 不使用 sms-activate 协议，桌面端不读取在线目录 —— "
                       + $"下方国家与档位取自配置 {section}.country / .target_price。"
                       + "如需更换，请在命令行查询该供应商的可用国家与价格档位。";
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
            if (!string.IsNullOrWhiteSpace(notice))
            {
                headingPanel.Children.Add(new TextBlock
                {
                    Text = notice,
                    FontSize = 12,
                    Foreground = (Brush)FindResource("TextMuted"),
                    TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(0, 8, 0, 0)
                });
            }
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
                    inventory.Text = tier.Count < 0
                        ? $"价格 ${tier.Price} / 个（取自配置，未查询库存）"
                        : $"当前库存 {tier.Count} 个，价格 ${tier.Price} / 个";
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

            if (!provider.CatalogIsSmsActivate)
            {
                // The two choices above were read *from* the config, so writing
                // them back could only reformat them. Skipping the write also
                // keeps `service_name` / `country_name_zh` -- two write-only
                // leaves -- out of this provider's section, which is what keeps
                // `tests/test_config_usage.py` green after this dialog runs.
                return true;
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

        /// <summary>
        /// Build a one-entry country/tier pair from the config, for providers
        /// whose catalog this dialog cannot read (see
        /// <see cref="SmsProviderCatalog.SmsProvider.CatalogIsSmsActivate"/>).
        ///
        /// Returns <c>null</c> when the config has no usable country or price --
        /// the caller turns that into a message naming the keys to fill in,
        /// rather than silently renting whatever the vendor defaults to.
        /// </summary>
        private SmsProviderCountryChoice? ReadSavedSmsProviderChoice(string section)
        {
            string country = settingsService.GetString(section + ".country")?.Trim() ?? "";
            string price = FirstNonEmpty(
                settingsService.GetString(section + ".target_price"),
                settingsService.GetString(section + ".max_price"),
                settingsService.GetString(section + ".min_price"));
            if (string.IsNullOrWhiteSpace(country) || string.IsNullOrWhiteSpace(price))
            {
                return null;
            }

            // `country_name` is optional: the backend resolves a numeric country
            // id through `phone_proxy.COUNTRY_ID_TO_ISO`, so a section that omits
            // the display name is correct, not incomplete. Fall back to the id
            // for display only -- and note we never write either name back.
            string englishName = FirstNonEmpty(settingsService.GetString(section + ".country_name"), country);
            string chineseName = settingsService.GetString(section + ".country_name_zh") ?? "";
            return new SmsProviderCountryChoice(
                country,
                englishName,
                chineseName,
                new[] { new SmsProviderPriceTier(price, SmsProviderPriceTier.UnknownCount) });
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
