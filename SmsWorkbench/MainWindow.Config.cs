namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Settings dialog and config persistence
        private void ShowConfigDialog()
        {
            string path = Path.Combine(rootDir, "config.json");
            EnsureConfigFile(path);
            var config = ReadJsonObject(path);
            var email = GetSection(config, "email_registration");
            var proxy = GetSection(config, "proxy");
            var paypal = GetSection(config, "paypal");
            var gopay = GetSection(config, "gopay");
            var protocolPayments = GetSection(config, "protocol_payments");
            var protocolMethods = GetChildSection(protocolPayments, "methods");
            var idealProtocol = GetChildSection(protocolMethods, "ideal");
            var pixProtocol = GetChildSection(protocolMethods, "pix");
            var kakaoProtocol = GetChildSection(protocolMethods, "kakao");
            var blikProtocol = GetChildSection(protocolMethods, "blik");
            var twintProtocol = GetChildSection(protocolMethods, "twint");
            var storage = GetSection(config, "storage");
            var output = GetSection(config, "output");
            var cpaMode = GetSection(config, "cpa_mode");
            var sub2api = GetSection(config, "sub2api");
            var codexOauth = GetSection(config, "codex_oauth");
            var phoneReuse = GetSection(config, "phone_reuse");
            var smsBower = GetChildSection(phoneReuse, "smsbower");

            var dialog = new Window
            {
                Title = "配置",
                Owner = this,
                Width = Math.Min(1100, SystemParameters.WorkArea.Width - 80),
                Height = Math.Min(780, SystemParameters.WorkArea.Height - 80),
                MinWidth = 920,
                MinHeight = 660,
                ResizeMode = ResizeMode.CanResize,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (System.Windows.Media.Brush)FindResource("AppBg")
            };

            var root = new Grid { Margin = new Thickness(16) };
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var content = new Grid();
            content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(210) });
            content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(16) });
            content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            Grid.SetRow(content, 0);
            root.Children.Add(content);

            var sidebar = new StackPanel();
            sidebar.Children.Add(new TextBlock
            {
                Text = "配置分类",
                FontSize = 13,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMuted"),
                Margin = new Thickness(4, 0, 0, 12)
            });
            var sidebarShell = new Border
            {
                Background = (Brush)FindResource("SidebarBg"),
                BorderBrush = (Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(12),
                Child = sidebar
            };
            Grid.SetColumn(sidebarShell, 0);
            content.Children.Add(sidebarShell);

            var host = new Grid();
            var hostScroll = new ScrollViewer
            {
                Content = host,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                Padding = new Thickness(0)
            };
            var hostShell = new Border
            {
                Background = (Brush)FindResource("PanelBg"),
                BorderBrush = (Brush)FindResource("Line"),
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(22),
                Child = hostScroll
            };
            Grid.SetColumn(hostShell, 2);
            content.Children.Add(hostShell);

            var fields = new Dictionary<string, TextBox>();
            var comboFields = new Dictionary<string, ComboBox>();
            var categories = new List<ConfigCategory>();

            var mailForm = AddConfigCategory(sidebar, host, categories, "邮箱", "邮箱池和 OTP 轮询配置。");
            int row = 0;
            AddConfigField(mailForm, fields, row++, "OTP轮询间隔秒", "otp_poll_interval", GetString(email, "otp_poll_interval"));
            AddConfigField(mailForm, fields, row++, "邮箱池文件", "token_file", GetString(email, "token_file"));

            var cfForm = AddConfigCategory(sidebar, host, categories, "CFWorker", "临时域名邮箱和 Cloudflare Worker 接入配置。");
            row = 0;
            AddConfigField(cfForm, fields, row++, "CFWorker URL", "cfworker_url", GetString(email, "cfworker_url"));
            AddConfigField(cfForm, fields, row++, "CFWorker 域名", "cfworker_domain", GetString(email, "cfworker_domain"));
            AddConfigField(cfForm, fields, row++, "CFWorker Admin Token", "cfworker_admin_token", GetString(email, "cfworker_admin_token"));
            AddConfigField(cfForm, fields, row++, "Cloudflare API Token", "cfworker_api_token", GetString(email, "cfworker_api_token"));

            var phoneForm = AddConfigCategory(sidebar, host, categories, "手机接码", "SMSBower 凭据和 Codex OAuth 接码高级设置。");
            row = 0;
            AddConfigField(phoneForm, fields, row++, "SMSBower API Key", "smsbower_api_key", GetString(smsBower, "api_key"));
            AddConfigField(phoneForm, fields, row++, "短信等待秒", "smsbower_sms_timeout", GetString(smsBower, "sms_timeout"));
            AddConfigField(phoneForm, fields, row++, "短信轮询间隔秒", "smsbower_sms_poll_interval", GetString(smsBower, "sms_poll_interval"));
            AddConfigField(phoneForm, fields, row++, "复用次数", "phone_max_reuse_count", GetString(phoneReuse, "max_reuse_count"));
            AddConfigField(phoneForm, fields, row++, "发码冷却秒", "phone_send_cooldown_seconds", GetString(phoneReuse, "send_cooldown_seconds"));
            AddConfigField(phoneForm, fields, row++, "发码重试次数", "phone_send_retry_attempts", GetString(phoneReuse, "send_retry_attempts"));
            AddConfigField(phoneForm, fields, row++, "发码重试延迟秒", "phone_send_retry_delay_seconds", GetString(phoneReuse, "send_retry_delay_seconds"));
            AddConfigField(phoneForm, fields, row++, "状态文件", "phone_state_file", GetString(phoneReuse, "state_file"));
            AddConfigField(phoneForm, fields, row++, "OAuth超时秒", "codex_registration_timeout", GetString(codexOauth, "registration_timeout"));
            AddConfigField(phoneForm, fields, row++, "允许邮箱OTP兜底", "codex_allow_passwordless_takeover", GetString(codexOauth, "allow_passwordless_takeover"));
            AddConfigField(phoneForm, fields, row++, "自动手机验证", "codex_auto_phone_verification", GetString(codexOauth, "auto_phone_verification"));
            AddConfigField(phoneForm, fields, row++, "注册要求RT", "codex_require_registration_refresh_token", GetString(codexOauth, "require_registration_refresh_token"));
            AddConfigField(phoneForm, fields, row++, "注册要求手机号", "codex_require_registration_phone_verification", GetString(codexOauth, "require_registration_phone_verification"));

            var cpaForm = AddConfigCategory(sidebar, host, categories, "CPA", "CPA 导入接口配置。");
            row = 0;
            AddConfigField(cpaForm, fields, row++, "CPA地址", "cpa_api_url", GetString(cpaMode, "api_url"));
            AddConfigField(cpaForm, fields, row++, "CPA Token", "cpa_api_token", GetString(cpaMode, "api_token"));
            var sub2Form = AddConfigCategory(sidebar, host, categories, "SUB2API", "SUB2API 导入、分组和代理配置。");
            row = 0;
            AddConfigField(sub2Form, fields, row++, "SUB2API地址", "sub2api_url", GetString(sub2api, "api_url"));
            AddConfigField(sub2Form, fields, row++, "SUB2API Token", "sub2api_token", GetString(sub2api, "api_token"));
            AddConfigField(sub2Form, fields, row++, "SUB2API邮箱", "sub2api_email", GetString(sub2api, "email"));
            AddConfigField(sub2Form, fields, row++, "SUB2API密码", "sub2api_password", GetString(sub2api, "password"));
            AddConfigField(sub2Form, fields, row++, "SUB2API分组", "sub2api_group", GetString(sub2api, "group_name"));
            AddConfigField(sub2Form, fields, row++, "SUB2API分组ID", "sub2api_group_ids", GetString(sub2api, "group_ids"));
            AddConfigField(sub2Form, fields, row++, "SUB2API代理", "sub2api_proxy", GetString(sub2api, "proxy_name"));
            AddConfigField(sub2Form, fields, row++, "SUB2API代理ID", "sub2api_proxy_id", GetString(sub2api, "proxy_id"));
            AddConfigField(sub2Form, fields, row++, "SUB2API优先级", "sub2api_priority", GetString(sub2api, "priority"));
            AddConfigField(sub2Form, fields, row++, "SUB2API并发", "sub2api_concurrency", GetString(sub2api, "concurrency"));

            var networkForm = AddConfigCategory(sidebar, host, categories, "网络代理", "注册、邮箱、接码、额度查询等非支付功能统一使用本地 7897 端口。");
            row = 0;
            AddConfigField(networkForm, fields, row++, "非支付代理（固定）", "non_payment_proxy", LocalNonPaymentProxy, isReadOnly: true);

            var proxyForm = AddConfigCategory(sidebar, host, categories, "协议支付", "统一管理 PayPal、GoPay、UPI、iDEAL、PIX、Kakao Pay、BLIK 和 TWINT 提链。");
            row = 0;
            AddConfigField(proxyForm, fields, row++, "启用方式", "protocol_enabled_methods", FirstNonEmpty(FormatConfigList(protocolPayments, "enabled_methods"), "paypal,gopay,upi,ideal,pix,kakao,blik,twint"));
            AddConfigField(proxyForm, fields, row++, "提链器目录", "protocol_reference_root", FirstNonEmpty(GetString(protocolPayments, "reference_root"), "services/protocol-payment"));
            AddConfigField(proxyForm, fields, row++, "状态文件", "protocol_state_file", FirstNonEmpty(GetString(protocolPayments, "state_file"), "runtime/payment_link_runs.jsonl"));
            AddConfigField(proxyForm, fields, row++, "协议超时秒", "protocol_timeout_seconds", FirstNonEmpty(GetString(protocolPayments, "timeout_seconds"), "900"));
            AddConfigField(proxyForm, fields, row++, "PayPal代理", "paypal_proxy", FirstListValue(paypal, "proxies"));
            AddConfigComboField(proxyForm, comboFields, row++, "订单生成地区", "paypal_billing_region", GetBillingRegionCode(paypal), BillingRegionOptions, "DE");
            AddConfigComboField(proxyForm, comboFields, row++, "PayPal直链生成模式", "paypal_link_generation_type", GetLinkGenerationType(paypal), LinkGenerationTypeOptions, "hosted_long_url");
            AddConfigField(proxyForm, fields, row++, "GoPay服务地址", "protocol_gopay_service_addr", FirstNonEmpty(GetString(gopay, "payment_service_addr"), "127.0.0.1:50051"));
            AddConfigField(proxyForm, fields, row++, "iDEAL代理Seed", "protocol_ideal_proxy", GetString(idealProtocol, "proxy"));
            AddConfigField(proxyForm, fields, row++, "PIX代理Seed", "protocol_pix_proxy", GetString(pixProtocol, "proxy"));
            AddConfigField(proxyForm, fields, row++, "Kakao Pay代理Seed", "protocol_kakao_proxy", GetString(kakaoProtocol, "proxy"));
            AddConfigField(proxyForm, fields, row++, "BLIK代理Seed", "protocol_blik_proxy", GetString(blikProtocol, "proxy"));
            AddConfigField(proxyForm, fields, row++, "BLIK六位码", "protocol_blik_code", GetString(blikProtocol, "blik_code"));
            AddConfigField(proxyForm, fields, row++, "TWINT代理Seed", "protocol_twint_proxy", GetString(twintProtocol, "proxy"));

            var storageForm = AddConfigCategory(sidebar, host, categories, "存储", "Session 输出目录和 SQLite 索引路径。");
            row = 0;
            AddConfigField(storageForm, fields, row++, "Session目录", "output_directory", GetString(output, "directory"));
            AddConfigField(storageForm, fields, row++, "SQLite路径", "sqlite_path", GetString(storage, "sqlite_path"));
            if (categories.Count > 0) SelectConfigCategory(categories, categories[0]);

            var actions = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
                Margin = new Thickness(0, 12, 0, 0)
            };
            var openJsonButton = new Button { Content = "打开JSON", Width = 120 };
            openJsonButton.Click += (_, __) => OpenPath(path);
            var saveButton = new Button { Content = "保存", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            saveButton.Click += (_, __) =>
            {
                email["otp_poll_interval"] = fields["otp_poll_interval"].Text.Trim();
                email["token_file"] = fields["token_file"].Text.Trim();
                email["cfworker_url"] = fields["cfworker_url"].Text.Trim();
                email["cfworker_domain"] = fields["cfworker_domain"].Text.Trim();
                email["cfworker_admin_token"] = fields["cfworker_admin_token"].Text.Trim();
                email["cfworker_api_token"] = fields["cfworker_api_token"].Text.Trim();
                smsBower["api_key"] = fields["smsbower_api_key"].Text.Trim();
                smsBower["service"] = "dr";
                smsBower["service_name"] = "OpenAI (ChatGPT)";
                smsBower.Remove("pool_size");
                smsBower["sms_timeout"] = ConfigIntegerValue(fields, "smsbower_sms_timeout");
                smsBower["sms_poll_interval"] = ConfigIntegerValue(fields, "smsbower_sms_poll_interval");
                phoneReuse["source"] = "smsbower";
                phoneReuse["smsbower"] = smsBower;
                phoneReuse["max_reuse_count"] = ConfigIntegerValue(fields, "phone_max_reuse_count");
                phoneReuse["send_cooldown_seconds"] = ConfigIntegerValue(fields, "phone_send_cooldown_seconds");
                phoneReuse["send_retry_attempts"] = ConfigIntegerValue(fields, "phone_send_retry_attempts");
                phoneReuse["send_retry_delay_seconds"] = ConfigIntegerValue(fields, "phone_send_retry_delay_seconds");
                phoneReuse["state_file"] = fields["phone_state_file"].Text.Trim();
                phoneReuse.Remove("phone_pool");
                codexOauth["registration_timeout"] = ConfigIntegerValue(fields, "codex_registration_timeout");
                codexOauth["allow_passwordless_takeover"] = ConfigBoolValue(fields, "codex_allow_passwordless_takeover", GetBool(codexOauth, "allow_passwordless_takeover", false));
                codexOauth["auto_phone_verification"] = ConfigBoolValue(fields, "codex_auto_phone_verification", GetBool(codexOauth, "auto_phone_verification", false));
                codexOauth["require_registration_refresh_token"] = ConfigBoolValue(fields, "codex_require_registration_refresh_token", GetBool(codexOauth, "require_registration_refresh_token", true));
                codexOauth["require_registration_phone_verification"] = ConfigBoolValue(fields, "codex_require_registration_phone_verification", GetBool(codexOauth, "require_registration_phone_verification", true));
                proxy["default"] = fields["default_proxy"].Text.Trim();
                paypal["proxies"] = new List<object> { fields["paypal_proxy"].Text.Trim() };
                paypal["billing_regions"] = new List<object> { ConfigComboOptionValue(comboFields, "paypal_billing_region", "DE").Value };
                paypal["link_generation_type"] = ConfigComboOptionValue(comboFields, "paypal_link_generation_type", "hosted_long_url").Value;
                protocolPayments["enabled_methods"] = ParseStringList(fields["protocol_enabled_methods"].Text);
                protocolPayments["reference_root"] = fields["protocol_reference_root"].Text.Trim();
                protocolPayments["state_file"] = fields["protocol_state_file"].Text.Trim();
                protocolPayments["timeout_seconds"] = ConfigIntegerValue(fields, "protocol_timeout_seconds");
                gopay["payment_service_addr"] = fields["protocol_gopay_service_addr"].Text.Trim();
                idealProtocol["proxy"] = fields["protocol_ideal_proxy"].Text.Trim();
                pixProtocol["proxy"] = fields["protocol_pix_proxy"].Text.Trim();
                kakaoProtocol["proxy"] = fields["protocol_kakao_proxy"].Text.Trim();
                blikProtocol["proxy"] = fields["protocol_blik_proxy"].Text.Trim();
                blikProtocol["blik_code"] = fields["protocol_blik_code"].Text.Trim();
                twintProtocol["proxy"] = fields["protocol_twint_proxy"].Text.Trim();
                protocolMethods["ideal"] = idealProtocol;
                protocolMethods["pix"] = pixProtocol;
                protocolMethods["kakao"] = kakaoProtocol;
                protocolMethods["blik"] = blikProtocol;
                protocolMethods["twint"] = twintProtocol;
                protocolPayments["methods"] = protocolMethods;
                output["directory"] = fields["output_directory"].Text.Trim();
                storage["sqlite_path"] = fields["sqlite_path"].Text.Trim();
                cpaMode["api_url"] = fields["cpa_api_url"].Text.Trim();
                cpaMode["api_token"] = fields["cpa_api_token"].Text.Trim();
                sub2api["api_url"] = fields["sub2api_url"].Text.Trim();
                sub2api["api_token"] = fields["sub2api_token"].Text.Trim();
                sub2api["email"] = fields["sub2api_email"].Text.Trim();
                sub2api["password"] = fields["sub2api_password"].Text.Trim();
                sub2api["group_name"] = fields["sub2api_group"].Text.Trim();
                sub2api["group_ids"] = fields["sub2api_group_ids"].Text.Trim();
                sub2api["proxy_name"] = fields["sub2api_proxy"].Text.Trim();
                sub2api["proxy_id"] = fields["sub2api_proxy_id"].Text.Trim();
                sub2api["priority"] = fields["sub2api_priority"].Text.Trim();
                sub2api["concurrency"] = fields["sub2api_concurrency"].Text.Trim();
                proxy["default"] = LocalNonPaymentProxy;
                proxy["pool"] = new List<object> { LocalNonPaymentProxy };
                config["mailbox_proxy"] = LocalNonPaymentProxy;
                phoneReuse["proxy"] = LocalNonPaymentProxy;
                phoneReuse["proxy_match_phone_country"] = false;
                phoneReuse["proxy_random_sid"] = false;
                phoneReuse.Remove("proxy_api_url");
                phoneReuse.Remove("white_api_url");
                phoneReuse.Remove("api_url");
                phoneReuse.Remove("proxy_template");
                phoneReuse.Remove("proxies");
                config["email_registration"] = email;
                config["proxy"] = proxy;
                config["paypal"] = paypal;
                config["gopay"] = gopay;
                config["protocol_payments"] = protocolPayments;
                config["output"] = output;
                config["storage"] = storage;
                config["cpa_mode"] = cpaMode;
                config["sub2api"] = sub2api;
                config["codex_oauth"] = codexOauth;
                config["phone_reuse"] = phoneReuse;
                SaveConfig(path, config);
                Log("配置已保存。");
                dialog.Close();
            };
            var cancelButton = new Button { Content = "取消", Width = 72 };
            cancelButton.Click += (_, __) => dialog.Close();
            actions.Children.Add(openJsonButton);
            actions.Children.Add(saveButton);
            actions.Children.Add(cancelButton);
            Grid.SetRow(actions, 1);
            root.Children.Add(actions);

            dialog.Content = root;
            dialog.ShowDialog();
        }

        private sealed class ConfigCategory
        {
            public Button Button { get; set; } = new Button();
            public FrameworkElement Panel { get; set; } = new StackPanel();
        }

        private sealed class ConfigComboOption
        {
            public ConfigComboOption(string value, string label, string metadata = "", string extra = "")
            {
                Value = value;
                Label = label;
                Metadata = metadata;
                Extra = extra;
            }

            public string Value { get; }
            public string Label { get; }
            public string Metadata { get; }
            public string Extra { get; }

            public override string ToString()
            {
                return Label;
            }
        }

        private Grid AddConfigCategory(StackPanel sidebar, Grid host, List<ConfigCategory> categories, string title, string description)
        {
            var button = new Button
            {
                Content = title,
                Style = (Style)FindResource("SidebarButton"),
                Width = double.NaN
            };

            var panel = new StackPanel
            {
                Visibility = Visibility.Collapsed
            };
            panel.Children.Add(new TextBlock
            {
                Text = title,
                FontSize = 20,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 6)
            });
            panel.Children.Add(new TextBlock
            {
                Text = description,
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 0, 0, 18)
            });

            var form = new Grid();
            form.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(220) });
            form.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            panel.Children.Add(form);
            host.Children.Add(panel);
            sidebar.Children.Add(button);

            var category = new ConfigCategory { Button = button, Panel = panel };
            categories.Add(category);
            button.Click += (_, __) => SelectConfigCategory(categories, category);
            return form;
        }

        private void SelectConfigCategory(List<ConfigCategory> categories, ConfigCategory selected)
        {
            foreach (ConfigCategory category in categories)
            {
                bool isSelected = ReferenceEquals(category, selected);
                category.Panel.Visibility = isSelected ? Visibility.Visible : Visibility.Collapsed;
                category.Button.Background = (Brush)FindResource(isSelected ? "PanelHover" : "PanelBg");
                category.Button.BorderBrush = (Brush)FindResource(isSelected ? "Primary" : "Line");
                category.Button.Foreground = (Brush)FindResource("TextMain");
            }
        }

        private void AddConfigField(
            Grid form,
            Dictionary<string, TextBox> fields,
            int row,
            string label,
            string key,
            string value,
            bool multiline = false,
            bool isReadOnly = false)
        {
            form.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var text = new TextBlock
            {
                Text = label,
                VerticalAlignment = VerticalAlignment.Top,
                TextWrapping = TextWrapping.Wrap,
                LineHeight = 18,
                LineStackingStrategy = LineStackingStrategy.BlockLineHeight,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 8, 14, 12)
            };
            Grid.SetRow(text, row);
            Grid.SetColumn(text, 0);
            form.Children.Add(text);

            var box = new TextBox
            {
                Text = value ?? "",
                Margin = new Thickness(0, 0, 0, 12),
                Padding = new Thickness(8, 5, 8, 5),
                AcceptsReturn = multiline,
                TextWrapping = multiline ? TextWrapping.NoWrap : TextWrapping.NoWrap,
                VerticalScrollBarVisibility = multiline ? ScrollBarVisibility.Auto : ScrollBarVisibility.Disabled,
                HorizontalScrollBarVisibility = multiline ? ScrollBarVisibility.Auto : ScrollBarVisibility.Disabled,
                MinHeight = multiline ? 124 : 36,
                VerticalContentAlignment = multiline ? VerticalAlignment.Top : VerticalAlignment.Center,
                IsReadOnly = isReadOnly
            };
            if (multiline)
            {
                box.FontFamily = new System.Windows.Media.FontFamily("Consolas");
            }
            Grid.SetRow(box, row);
            Grid.SetColumn(box, 1);
            form.Children.Add(box);
            fields[key] = box;
        }

        private void AddConfigComboField(Grid form, Dictionary<string, ComboBox> fields, int row, string label, string key, string value, IEnumerable<string> options)
        {
            form.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var text = new TextBlock
            {
                Text = label,
                VerticalAlignment = VerticalAlignment.Top,
                TextWrapping = TextWrapping.Wrap,
                LineHeight = 18,
                LineStackingStrategy = LineStackingStrategy.BlockLineHeight,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 8, 14, 12)
            };
            Grid.SetRow(text, row);
            Grid.SetColumn(text, 0);
            form.Children.Add(text);

            var combo = new ComboBox
            {
                Margin = new Thickness(0, 0, 0, 12),
                Padding = new Thickness(8, 4, 8, 4),
                MinHeight = 36,
                IsEditable = false,
                VerticalContentAlignment = VerticalAlignment.Center
            };
            string selected = FirstNonEmpty(value, "smsbower").Trim();
            bool matched = false;
            foreach (string option in options)
            {
                combo.Items.Add(option);
                if (option.Equals(selected, StringComparison.OrdinalIgnoreCase))
                {
                    combo.SelectedItem = option;
                    matched = true;
                }
            }
            if (!matched && combo.Items.Count > 0)
            {
                combo.SelectedIndex = 0;
            }
            Grid.SetRow(combo, row);
            Grid.SetColumn(combo, 1);
            form.Children.Add(combo);
            fields[key] = combo;
        }

        private void AddConfigComboField(Grid form, Dictionary<string, ComboBox> fields, int row, string label, string key, string value, IEnumerable<ConfigComboOption> options, string fallback)
        {
            form.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var text = new TextBlock
            {
                Text = label,
                VerticalAlignment = VerticalAlignment.Top,
                TextWrapping = TextWrapping.Wrap,
                LineHeight = 18,
                LineStackingStrategy = LineStackingStrategy.BlockLineHeight,
                Foreground = (System.Windows.Media.Brush)FindResource("TextSub"),
                Margin = new Thickness(0, 8, 14, 12)
            };
            Grid.SetRow(text, row);
            Grid.SetColumn(text, 0);
            form.Children.Add(text);

            var combo = new ComboBox
            {
                Margin = new Thickness(0, 0, 0, 12),
                Padding = new Thickness(8, 4, 8, 4),
                MinHeight = 36,
                IsEditable = false,
                VerticalContentAlignment = VerticalAlignment.Center
            };
            string selected = FirstNonEmpty(value, fallback).Trim();
            bool matched = false;
            foreach (ConfigComboOption option in options)
            {
                combo.Items.Add(option);
                if (option.Value.Equals(selected, StringComparison.OrdinalIgnoreCase))
                {
                    combo.SelectedItem = option;
                    matched = true;
                }
            }
            if (!matched && combo.Items.Count > 0)
            {
                combo.SelectedIndex = 0;
            }
            Grid.SetRow(combo, row);
            Grid.SetColumn(combo, 1);
            form.Children.Add(combo);
            fields[key] = combo;
        }

        private Dictionary<string, object> GetSection(Dictionary<string, object> config, string section)
        {
            if (config.TryGetValue(section, out object value) && value is Dictionary<string, object> map)
            {
                return map;
            }
            var created = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            config[section] = created;
            return created;
        }

        private Dictionary<string, object> GetChildSection(Dictionary<string, object> parent, string key)
        {
            if (parent.TryGetValue(key, out object value) && value is Dictionary<string, object> map)
            {
                return map;
            }
            var created = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            parent[key] = created;
            return created;
        }

        private object ConfigIntegerValue(Dictionary<string, TextBox> fields, string key)
        {
            string raw = fields.TryGetValue(key, out TextBox box) ? box.Text.Trim() : "";
            if (int.TryParse(raw, out int value)) return value;
            return raw;
        }

        private string ConfigComboValue(Dictionary<string, ComboBox> fields, string key, string fallback)
        {
            if (!fields.TryGetValue(key, out ComboBox combo)) return fallback;
            return Convert.ToString(combo.SelectedItem) ?? fallback;
        }

        private ConfigComboOption ConfigComboOptionValue(Dictionary<string, ComboBox> fields, string key, string fallback)
        {
            if (fields.TryGetValue(key, out ComboBox combo))
            {
                if (combo.SelectedItem is ConfigComboOption selected)
                {
                    return selected;
                }
                return combo.Items.OfType<ConfigComboOption>()
                    .FirstOrDefault(option => option.Value.Equals(fallback, StringComparison.OrdinalIgnoreCase))
                    ?? combo.Items.OfType<ConfigComboOption>().FirstOrDefault()
                    ?? new ConfigComboOption(fallback, fallback, fallback, fallback);
            }
            return new ConfigComboOption(fallback, fallback, fallback, fallback);
        }

        private bool ConfigBoolValue(Dictionary<string, TextBox> fields, string key, bool fallback)
        {
            string raw = fields.TryGetValue(key, out TextBox box) ? box.Text.Trim() : "";
            if (raw.Length == 0) return fallback;
            if (raw.Equals("true", StringComparison.OrdinalIgnoreCase) || raw == "1" || raw.Equals("yes", StringComparison.OrdinalIgnoreCase) || raw.Equals("on", StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
            if (raw.Equals("false", StringComparison.OrdinalIgnoreCase) || raw == "0" || raw.Equals("no", StringComparison.OrdinalIgnoreCase) || raw.Equals("off", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }
            return fallback;
        }

        private bool GetBool(Dictionary<string, object> data, string key, bool fallback)
        {
            if (!data.TryGetValue(key, out object value) || value == null) return fallback;
            if (value is bool flag) return flag;
            string raw = Convert.ToString(value) ?? "";
            if (raw.Equals("true", StringComparison.OrdinalIgnoreCase) || raw == "1" || raw.Equals("yes", StringComparison.OrdinalIgnoreCase) || raw.Equals("on", StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
            if (raw.Equals("false", StringComparison.OrdinalIgnoreCase) || raw == "0" || raw.Equals("no", StringComparison.OrdinalIgnoreCase) || raw.Equals("off", StringComparison.OrdinalIgnoreCase))
            {
                return false;
            }
            return fallback;
        }

        private string FormatConfigList(Dictionary<string, object> data, string key)
        {
            if (!data.TryGetValue(key, out object value) || value == null)
            {
                return "";
            }
            if (value is List<object> list)
            {
                return string.Join(",", list.Select(item => Convert.ToString(item) ?? "").Where(item => item.Length > 0));
            }
            return Convert.ToString(value) ?? "";
        }

        private List<object> ParseStringList(string raw)
        {
            return (raw ?? "")
                .Split(new[] { "\r\n", "\n", "," }, StringSplitOptions.RemoveEmptyEntries)
                .Select(item => item.Trim())
                .Where(item => item.Length > 0)
                .Cast<object>()
                .ToList();
        }

        private string FirstListValue(Dictionary<string, object> data, string key)
        {
            if (data.TryGetValue(key, out object value) && value is List<object> list && list.Count > 0)
            {
                return Convert.ToString(list[0]) ?? "";
            }
            return "";
        }

        private string GetBillingRegionCode(Dictionary<string, object> paypal)
        {
            string value = FirstListValue(paypal, "billing_regions").Trim();
            if (value.Length == 0)
            {
                value = FirstNonEmpty(GetString(paypal, "billing_region"), GetString(paypal, "billing_country"), "DE");
            }
            value = value.Trim().ToUpperInvariant();
            if (BillingRegionOptions.Any(option => option.Value.Equals(value, StringComparison.OrdinalIgnoreCase)))
            {
                return value;
            }
            return "DE";
        }

        private string GetLinkGenerationType(Dictionary<string, object> paypal)
        {
            string value = GetString(paypal, "link_generation_type").Trim();
            if (LinkGenerationTypeOptions.Any(option => option.Value.Equals(value, StringComparison.OrdinalIgnoreCase)))
            {
                return value;
            }
            return "hosted_long_url";
        }

        private void SaveConfig(string path, Dictionary<string, object> config)
        {
            var options = new JsonSerializerOptions { WriteIndented = true };
            File.WriteAllText(path, JsonSerializer.Serialize(config, options), Encoding.UTF8);
        }

        private void EnsureConfigFile(string path)
        {
            if (File.Exists(path)) return;
            string example = Path.Combine(rootDir, "config.example.json");
            if (File.Exists(example))
            {
                File.Copy(example, path);
            }
            else
            {
                File.WriteAllText(path, "{}", Encoding.UTF8);
            }
        }
    }
}
