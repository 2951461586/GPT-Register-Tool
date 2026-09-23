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
        /// 🔴 The dialog is *not* provider-agnostic, and the protocol is what
        /// decides how the catalog is read. There are two families, each with
        /// its own reader in <see cref="SmsProviderCatalogClient"/>:
        /// </para>
        ///
        /// <list type="bullet">
        /// <item><description>
        /// **sms-activate** (`smsbower` / `herosms` / `grizzly`) --
        /// `?action=getCountries` / `getPricesV3`, balance via
        /// `ACCESS_BALANCE:`.
        /// </description></item>
        /// <item><description>
        /// **nexsms** -- `/api/countries` + `/api/getCountryByService`, balance
        /// via `/api/balance`, all wrapped in `{code, message, data}`.
        /// </description></item>
        /// </list>
        ///
        /// <para>
        /// Both families are read **online**. nexsms used to take its country
        /// and price tier from the config instead, on the argument that
        /// reimplementing its REST catalog in C# would be a second copy of a
        /// protocol `sms_tool` already implements. That argument was right about
        /// the *protocol* and wrong about the *shape*: reading it here needs one
        /// GET and a `priceMap` walk, while the config path cost the operator the
        /// whole point of the dialog -- a real balance, every country the vendor
        /// serves, and the actual per-tier stock instead of one hard-coded pair.
        /// </para>
        ///
        /// <para>
        /// 🔴 The config fallback is still not protocol-specific: **any** failure
        /// to read the online catalog lands there, not just a protocol mismatch.
        /// The catalog is an enhancement -- it is what gives the operator a
        /// dropdown of every country and tier -- but it is not a precondition for
        /// renting. Treating it as one is what made a single 404 (`herosms`
        /// answers `getPricesV3` with 404 and serves only `getPrices`) or a
        /// single 403 read as "this vendor is unusable", when the vendor was
        /// fine. The reason for the failure is still surfaced in the dialog; it
        /// just no longer ends the flow.
        /// </para>
        ///
        /// <para>
        /// A config-derived choice is never written back. The values shown *are*
        /// the config values, so a write could only reformat them -- and it would
        /// add `service_name` / `country_name_zh`, two write-only leaves whose
        /// appearance in a non-`smsbower` section turns
        /// `tests/test_config_usage.py` red (see `docs/TROUBLESHOOTING.md` §13).
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

            // Read once, before the branch: the "online lookup failed" fallback
            // needs it, and reading it twice would let the two paths disagree if
            // the config changed mid-dialog.
            SmsProviderCountryChoice? savedChoice = ReadSavedSmsProviderChoice(section);

            string endpoint = FirstNonEmpty(settingsService.GetString(section + ".endpoint"), provider.DefaultEndpoint);
            string service = FirstNonEmpty(
                settingsService.GetString(section + ".service"), SmsProviderCatalogClient.OpenAiService);

            // The online read, whichever family this provider speaks. One block
            // rather than one per family: the two differ only in which reader is
            // called, and duplicating the wait-cursor / error-capture / fallback
            // handling per family is how the two would drift out of step.
            IReadOnlyList<SmsProviderCountryChoice>? online = null;
            string balance = "--";
            string catalogError = "";
            try
            {
                System.Windows.Input.Mouse.OverrideCursor = System.Windows.Input.Cursors.Wait;
                online = provider.CatalogIsSmsActivate
                    ? await SmsProviderCatalogClient.LoadOpenAiCatalogAsync(httpClient, apiKey, endpoint)
                    : await SmsProviderCatalogClient.LoadNexsmsCatalogAsync(
                        httpClient, apiKey, endpoint, service);

                if (online.Count == 0)
                {
                    catalogError = "在线目录没有返回任何可用国家";
                    online = null;
                }
                else
                {
                    try
                    {
                        balance = provider.CatalogIsSmsActivate
                            ? await SmsProviderCatalogClient.LoadBalanceAsync(httpClient, apiKey, endpoint)
                            : await SmsProviderCatalogClient.LoadNexsmsBalanceAsync(httpClient, apiKey, endpoint);
                    }
                    catch (Exception balanceError)
                    {
                        // A balance that cannot be read is not a failed catalog:
                        // the operator can still rent, they just cannot see what
                        // they have left. Logged, and the header keeps its "--".
                        logger?.Warning(balanceError, "Failed to load {Provider} balance", provider.Label);
                    }
                }
            }
            catch (Exception exc)
            {
                logger?.Error(exc, "Failed to load {Provider} OpenAI catalog", provider.Label);
                catalogError = exc.Message;
            }
            finally
            {
                System.Windows.Input.Mouse.OverrideCursor = null;
            }

            IReadOnlyList<SmsProviderCountryChoice> countries;
            bool fromCatalog = online is not null;
            if (online is not null)
            {
                countries = online;
            }
            else if (savedChoice is not null)
            {
                // 🔴 The online lookup is an *enhancement*, not a precondition.
                // It fails for reasons that say nothing about whether the vendor
                // can rent: herosms answers `getPricesV3` with 404, a vendor can
                // be briefly down, a proxy can be filtering. Dead-ending here is
                // what turned "该供应商的在线目录读不到" into "该供应商完全不可用"
                // -- and the backend reads the same two config keys anyway, so
                // the config values are exactly what a rental would use.
                //
                // The failure is still surfaced, by `ShowThemedInfoDialog` below.
                // Falling back silently would let a stale config look freshly
                // verified.
                countries = new[] { savedChoice };
            }
            else
            {
                ShowThemedInfoDialog(
                    provider.Label + " 加载失败",
                    $"无法读取 OpenAI 号码地区和价格档位：{catalogError}"
                    + $"。配置里也没有可用的国家与档位，请先填写 {section}.country 与"
                    + $" {section}.target_price（或 .max_price / .min_price），"
                    + $"或改用命令行查询 {provider.Label} 的可用国家与价格。");
                return false;
            }

            if (!fromCatalog && !string.IsNullOrWhiteSpace(catalogError))
            {
                // Non-fatal, so it is reported rather than shown as a dialog that
                // has to be dismissed before the dropdown can be used.
                logger?.Warning(
                    "{Provider} catalog unavailable ({Error}); falling back to the configured country and tier",
                    provider.Label, catalogError);
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
                if (tierBox.SelectedItem is not SmsProviderPriceTier tier)
                {
                    inventory.Text = "";
                    return;
                }

                // `Count < 0` is `SmsProviderPriceTier.UnknownCount`: the tier came
                // from the config, so there is no stock figure to show. Only the
                // price is printed -- an "(unqueried)" note next to it read as a
                // caveat on the price itself, which it never was.
                inventory.Text = tier.Count < 0
                    ? $"价格 ${tier.Price} / 个"
                    : $"当前库存 {tier.Count} 个，价格 ${tier.Price} / 个";
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

            if (!fromCatalog)
            {
                // The two choices above were read *from* the config -- because
                // the online catalog could not be read. Writing them back could
                // only reformat them, and the reformat is not free: `min_price` /
                // `max_price` / `target_price` would all be collapsed onto the one
                // configured value, so a section that deliberately kept a window
                // open would lose it. Skipping the write also keeps
                // `service_name` / `country_name_zh` -- two write-only leaves --
                // out of a section that did not already have them, which is what
                // keeps `tests/test_config_usage.py`'s counter-example honest
                // after this dialog runs.
                return true;
            }

            // 🔴 `provider_ids` is deliberately **removed** rather than written
            // when the tier has none -- the two protocols disagree about whether
            // the field means anything. On an sms-activate tier it names the
            // sub-provider to buy from; nexsms has no operator dimension at all
            // (`sms_tool/nexsms.py::get_number` logs and ignores it). Writing an
            // empty string would hand the backend a field it would read, find
            // blank, and have to re-derive -- leaving the key absent is the
            // state both ends already agree means "no preference".
            settingsService.UpdateConfig(root =>
            {
                JsonObject providerSection = GetOrCreateSection(GetOrCreateSection(root, "phone_reuse"), provider.Key);
                providerSection["service"] = service;
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
        /// Build a one-entry country/tier pair from the config, for the case
        /// where the online catalog could not be read.
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
