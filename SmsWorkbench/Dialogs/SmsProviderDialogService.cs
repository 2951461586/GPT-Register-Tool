// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using System.Text.Json.Nodes;

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// The one-click SMS rental dialog: provider catalog, country/tier choice and
    /// the config write-back of the operator's choice.
    /// </summary>
    /// <remarks>
    /// Split from <c>MainWindow.SmsProvider.cs</c> (2026-11-01). The window keeps a
    /// one-line delegate so <c>MainWindow.Register.cs</c> did not change. The three
    /// dependencies are everything this dialog needs from the window: settings (read
    /// and write), one shared <see cref="HttpClient"/> for the vendor catalog, the
    /// logger, and the themed info dialog.
    /// </remarks>
    public interface ISmsProviderDialogService
    {
        /// <summary>Returns false when the operator cancels; true when a provider/tier was chosen and saved.</summary>
        Task<bool> ShowOneClickDialogAsync(Window owner, CancellationToken ct = default);
    }

    public sealed class SmsProviderDialogService : ISmsProviderDialogService
    {
        private readonly ISettingsService settings;
        private readonly IDesktopReadClient smsRead;
        private readonly Serilog.ILogger logger;
        private readonly Action<string, string> showInfo;

        public SmsProviderDialogService(
            ISettingsService settings,
            IDesktopReadClient smsRead,
            Serilog.ILogger logger,
            Action<string, string> showInfo)
        {
            this.settings = settings;
            this.smsRead = smsRead;
            this.logger = logger;
            this.showInfo = showInfo;
        }

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
        /// its own reader in `sms_tool/providers/sms_vendor_api.py`:
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
        /// `tests/config/test_config_usage.py` red (see `docs/TROUBLESHOOTING.md` §13).
        /// </para>
        /// </summary>
        public async Task<bool> ShowOneClickDialogAsync(Window owner, CancellationToken ct = default)
        {
            SmsProviderCatalog.SmsProvider provider =
                SmsProviderCatalog.Resolve(settings.GetString("phone_reuse.source"));
            string section = "phone_reuse." + provider.Key;
            string apiKey = ResolveSmsProviderApiKey(settings.GetString(section + ".api_key"), provider);
            if (string.IsNullOrWhiteSpace(apiKey))
            {
                showInfo(
                    Loc.F("{0} 未配置", provider.Label),
                    Loc.F("请先在设置的「接码供应商」分类中填写 {0} 的 API Key（配置键 {1}.api_key，或环境变量 {2}）。", provider.Label, section, provider.ApiKeyEnv));
                return false;
            }

            // Read once, before the branch: the "online lookup failed" fallback
            // needs it, and reading it twice would let the two paths disagree if
            // the config changed mid-dialog.
            SmsProviderCountryChoice? savedChoice = ReadSavedSmsProviderChoice(section);

            string endpoint = BackendJson.FirstNonEmpty(settings.GetString(section + ".endpoint"), provider.DefaultEndpoint);
            string service = BackendJson.FirstNonEmpty(
                settings.GetString(section + ".service"), SmsProviderCatalog.OpenAiService);

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
                // P7-B: the vendor read happens in ``sms_tool`` and arrives over the
                // resident channel, so the desktop no longer owns a second copy of
                // either wire format (and no longer needs its own egress for this).
                online = SmsVendorPayloadMapper.ParseCatalog(
                    await smsRead.ReadProviderCatalogAsync(provider.Key, service, ct).ConfigureAwait(true));

                if (online.Count == 0)
                {
                    catalogError = Loc.T("在线目录没有返回任何可用国家");
                    online = null;
                }
                else
                {
                    try
                    {
                        balance = SmsVendorPayloadMapper.ParseBalance(
                            await smsRead.ReadProviderBalanceAsync(provider.Key, service, ct).ConfigureAwait(true));
                        if (balance.Length == 0) balance = "--";
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
                showInfo(
                    Loc.F("{0} 加载失败", provider.Label),
                    Loc.F("无法读取 OpenAI 号码地区和价格档位：{0}。配置里也没有可用的国家与档位，请先填写 {1}.country 与 {2}.target_price（或 .max_price / .min_price），或改用命令行查询 {0} 的可用国家与价格。", catalogError, section, section));
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

            string savedCountry = BackendJson.FirstNonEmpty(settings.GetString(section + ".country"), "38");
            string savedPrice = BackendJson.FirstNonEmpty(
                settings.GetString(section + ".target_price"),
                settings.GetString(section + ".max_price"),
                settings.GetString(section + ".min_price"));
            var selectedCountry = countries.FirstOrDefault(item => item.Id == savedCountry) ?? countries[0];
            var selectedTier = selectedCountry.Tiers.FirstOrDefault(item => PriceEquals(item.Price, savedPrice))
                ?? selectedCountry.Tiers[0];

            var viewModel = new SmsProviderDialogViewModel(
                provider.Label,
                balance,
                countries,
                selectedCountry,
                selectedTier);
            var dialog = new SmsProviderDialog(owner, viewModel);
            if (dialog.ShowDialog() != true
                || viewModel.ChosenCountry is not { } chosenCountry
                || viewModel.ChosenTier is not { } chosenTier)
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
                // keeps `tests/config/test_config_usage.py`'s counter-example honest
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
            settings.UpdateConfig(root =>
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
            string country = settings.GetString(section + ".country")?.Trim() ?? "";
            string price = BackendJson.FirstNonEmpty(
                settings.GetString(section + ".target_price"),
                settings.GetString(section + ".max_price"),
                settings.GetString(section + ".min_price"));
            if (string.IsNullOrWhiteSpace(country) || string.IsNullOrWhiteSpace(price))
            {
                return null;
            }

            // `country_name` is optional: the backend resolves a numeric country
            // id through `phone_proxy.COUNTRY_ID_TO_ISO`, so a section that omits
            // the display name is correct, not incomplete. Fall back to the id
            // for display only -- and note we never write either name back.
            string englishName = BackendJson.FirstNonEmpty(settings.GetString(section + ".country_name"), country);
            string chineseName = settings.GetString(section + ".country_name_zh") ?? "";
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
