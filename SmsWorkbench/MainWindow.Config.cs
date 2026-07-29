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
            var remail = GetChildSection(email, "remail");
            var proxy = GetSection(config, "proxy");
            var paypal = GetSection(config, "paypal");
            var protocolPayments = GetSection(config, "protocol_payments");
            string registrationProxy = FirstNonEmpty(
                GetString(proxy, "registration"),
                GetString(config, "registration_proxy"),
                FirstListValue(paypal, "proxies"),
                GetString(proxy, "default"),
                LocalNonPaymentProxy);
            string mailboxProxy = FirstNonEmpty(
                GetString(config, "mailbox_proxy"),
                GetString(email, "mailbox_proxy"),
                GetString(proxy, "mailbox"),
                LocalNonPaymentProxy);
            string registrationProxyPool = FirstNonEmpty(
                FormatConfigList(proxy, "pool"),
                registrationProxy);
            string protocolProxyPool = FormatConfigList(protocolPayments, "proxy_pool");
            var gopay = GetSection(config, "gopay");
            var protocolMethods = GetChildSection(protocolPayments, "methods");
            var idealProtocol = GetChildSection(protocolMethods, "ideal");
            var pixProtocol = GetChildSection(protocolMethods, "pix");
            var kakaoProtocol = GetChildSection(protocolMethods, "kakao");
            var blikProtocol = GetChildSection(protocolMethods, "blik");
            var twintProtocol = GetChildSection(protocolMethods, "twint");
            var directCardProtocol = GetChildSection(protocolMethods, "direct_card");
            var momoProtocol = GetChildSection(protocolMethods, "momo");
            var storage = GetSection(config, "storage");
            var output = GetSection(config, "output");
            var cpaMode = GetSection(config, "cpa_mode");
            var sub2api = GetSection(config, "sub2api");
            var agentIdentity = GetSection(config, "agent_identity");
            var codexOauth = GetSection(config, "codex_oauth");
            var phoneReuse = GetSection(config, "phone_reuse");
            var smsBower = GetChildSection(phoneReuse, "smsbower");

            var dialog = new Window
            {
                Title = "设置",
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
            content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(190) });
            content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(16) });
            content.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            Grid.SetRow(content, 0);
            root.Children.Add(content);

            var sidebar = new StackPanel();
            sidebar.Children.Add(new TextBlock
            {
                Text = "设置分类",
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

            var mailForm = AddConfigCategory(sidebar, host, categories, "邮箱与收信", "统一管理邮箱池、ReMail 和 CFWorker 收信来源。");
            int row = 0;
            AddConfigSectionHeader(mailForm, row++, "邮箱池", "账号来源文件与 OTP 轮询节奏");
            AddConfigField(mailForm, fields, row++, "OTP轮询间隔秒", "otp_poll_interval", GetString(email, "otp_poll_interval"));
            AddConfigField(mailForm, fields, row++, "邮箱池文件", "token_file", GetString(email, "token_file"));
            AddConfigSectionHeader(mailForm, row++, "ReMail", "短效接码与长效邮箱库存");
            AddConfigField(mailForm, fields, row++, "启用", "remail_enabled", FirstNonEmpty(GetString(remail, "enabled"), "true"));
            AddConfigField(mailForm, fields, row++, "API地址", "remail_base_url", FirstNonEmpty(GetString(remail, "base_url"), "https://remail.aishop6.com"));
            AddConfigField(mailForm, fields, row++, "API Key", "remail_api_key", GetString(remail, "api_key"));
            AddConfigField(mailForm, fields, row++, "项目ID", "remail_project_id", FirstNonEmpty(GetString(remail, "project_id"), "2"));
            AddConfigField(mailForm, fields, row++, "产品ID", "remail_product_id", FirstNonEmpty(GetString(remail, "product_id"), "5"));
            AddConfigField(mailForm, fields, row++, "库存策略", "remail_supply", FirstNonEmpty(GetString(remail, "supply"), "private_first"));
            AddConfigField(mailForm, fields, row++, "邮箱后缀", "remail_email_suffix", FirstNonEmpty(GetString(remail, "email_suffix"), "outlook.com"));
            AddConfigSectionHeader(mailForm, row++, "CFWorker", "临时域名邮箱与 Cloudflare Worker 接入");
            AddConfigField(mailForm, fields, row++, "Worker URL", "cfworker_url", GetString(email, "cfworker_url"));
            AddConfigField(mailForm, fields, row++, "邮箱域名", "cfworker_domain", GetString(email, "cfworker_domain"));
            AddConfigField(mailForm, fields, row++, "Admin Token", "cfworker_admin_token", GetString(email, "cfworker_admin_token"));
            AddConfigField(mailForm, fields, row++, "Cloudflare API Token", "cfworker_api_token", GetString(email, "cfworker_api_token"));

            var phoneForm = AddConfigCategory(sidebar, host, categories, "注册与接码", "集中管理 SMSBower 和 Codex OAuth 验证策略。");
            row = 0;
            AddConfigSectionHeader(phoneForm, row++, "SMSBower", "短信获取、复用和重试策略");
            AddConfigField(phoneForm, fields, row++, "SMSBower API Key", "smsbower_api_key", GetString(smsBower, "api_key"));
            AddConfigField(phoneForm, fields, row++, "短信等待秒", "smsbower_sms_timeout", GetString(smsBower, "sms_timeout"));
            AddConfigField(phoneForm, fields, row++, "短信轮询间隔秒", "smsbower_sms_poll_interval", GetString(smsBower, "sms_poll_interval"));
            AddConfigField(phoneForm, fields, row++, "复用次数", "phone_max_reuse_count", GetString(phoneReuse, "max_reuse_count"));
            AddConfigField(phoneForm, fields, row++, "发码冷却秒", "phone_send_cooldown_seconds", GetString(phoneReuse, "send_cooldown_seconds"));
            AddConfigField(phoneForm, fields, row++, "发码重试次数", "phone_send_retry_attempts", GetString(phoneReuse, "send_retry_attempts"));
            AddConfigField(phoneForm, fields, row++, "发码重试延迟秒", "phone_send_retry_delay_seconds", GetString(phoneReuse, "send_retry_delay_seconds"));
            AddConfigField(phoneForm, fields, row++, "状态文件", "phone_state_file", GetString(phoneReuse, "state_file"));
            AddConfigSectionHeader(phoneForm, row++, "Codex OAuth", "注册后的令牌和手机验证要求");
            AddConfigField(phoneForm, fields, row++, "OAuth超时秒", "codex_registration_timeout", GetString(codexOauth, "registration_timeout"));
            AddConfigField(phoneForm, fields, row++, "允许邮箱OTP兜底", "codex_allow_passwordless_takeover", GetString(codexOauth, "allow_passwordless_takeover"));
            AddConfigField(phoneForm, fields, row++, "自动手机验证", "codex_auto_phone_verification", GetString(codexOauth, "auto_phone_verification"));
            AddConfigField(phoneForm, fields, row++, "注册要求RT", "codex_require_registration_refresh_token", GetString(codexOauth, "require_registration_refresh_token"));
            AddConfigField(phoneForm, fields, row++, "注册要求手机号", "codex_require_registration_phone_verification", GetString(codexOauth, "require_registration_phone_verification"));

            var importForm = AddConfigCategory(sidebar, host, categories, "导入与账号", "CPA、SUB2API 和 Agent Identity 统一配置。");
            row = 0;
            AddConfigSectionHeader(importForm, row++, "CPA", "兼容 CPA 的账号导入接口");
            AddConfigField(importForm, fields, row++, "CPA地址", "cpa_api_url", GetString(cpaMode, "api_url"));
            AddConfigField(importForm, fields, row++, "CPA Token", "cpa_api_token", GetString(cpaMode, "api_token"));
            AddConfigSectionHeader(importForm, row++, "SUB2API", "导入目标、分组和远端代理");
            AddConfigField(importForm, fields, row++, "API地址", "sub2api_url", GetString(sub2api, "api_url"));
            AddConfigField(importForm, fields, row++, "API Token", "sub2api_token", GetString(sub2api, "api_token"));
            AddConfigField(importForm, fields, row++, "登录邮箱", "sub2api_email", GetString(sub2api, "email"));
            AddConfigField(importForm, fields, row++, "登录密码", "sub2api_password", GetString(sub2api, "password"));
            AddConfigField(importForm, fields, row++, "目标分组", "sub2api_group", GetString(sub2api, "group_name"));
            AddConfigField(importForm, fields, row++, "分组ID", "sub2api_group_ids", GetString(sub2api, "group_ids"));
            AddConfigField(importForm, fields, row++, "远端代理", "sub2api_proxy", GetString(sub2api, "proxy_name"));
            AddConfigField(importForm, fields, row++, "代理ID", "sub2api_proxy_id", GetString(sub2api, "proxy_id"));
            AddConfigField(importForm, fields, row++, "优先级", "sub2api_priority", GetString(sub2api, "priority"));
            AddConfigField(importForm, fields, row++, "账号并发", "sub2api_concurrency", GetString(sub2api, "concurrency"));
            AddConfigComboField(importForm, comboFields, row++, "凭据模式", "sub2api_auth_mode", FirstNonEmpty(GetString(sub2api, "auth_mode"), "auto"), new[] { "auto", "oauth", "agent_identity" });
            AddConfigField(importForm, fields, row++, "导入后连通测试", "sub2api_verify_after_import", FirstNonEmpty(GetString(sub2api, "verify_after_import"), "true"));
            AddConfigSectionHeader(importForm, row++, "Agent Identity", "Free 注册后的 Agent 凭据生成");
            AddConfigField(importForm, fields, row++, "注册后自动生成", "agent_identity_register_on_free_signup", FirstNonEmpty(GetString(agentIdentity, "register_on_free_signup"), "false"));
            AddConfigField(importForm, fields, row++, "注册超时秒", "agent_identity_registration_timeout", FirstNonEmpty(GetString(agentIdentity, "registration_timeout"), "30"));

            var networkForm = AddConfigCategory(sidebar, host, categories, "网络与支付", "集中管理非支付网络、支付代理和协议提链器。");
            row = 0;
            AddConfigSectionHeader(networkForm, row++, "基础网络", "注册流量与邮箱收件流量分开配置");
            AddConfigField(networkForm, fields, row++, "注册代理（主）", "registration_proxy", registrationProxy);
            AddConfigField(networkForm, fields, row++, "注册代理池", "registration_proxy_pool", registrationProxyPool, multiline: true);
            AddConfigField(networkForm, fields, row++, "邮箱收件代理", "mailbox_proxy", mailboxProxy);
            AddConfigSectionHeader(networkForm, row++, "协议管理", "支付方式、提链器和运行状态");
            AddConfigField(networkForm, fields, row++, "协议支付代理池", "protocol_proxy_pool", protocolProxyPool, multiline: true);
            AddConfigField(networkForm, fields, row++, "启用方式", "protocol_enabled_methods", FirstNonEmpty(FormatConfigList(protocolPayments, "enabled_methods"), "paypal,gopay,upi,ideal,pix,kakao,blik,twint,direct_card,momo"));
            AddConfigField(networkForm, fields, row++, "提链器目录", "protocol_reference_root", FirstNonEmpty(GetString(protocolPayments, "reference_root"), "services/protocol-payment"));
            AddConfigField(networkForm, fields, row++, "状态文件", "protocol_state_file", FirstNonEmpty(GetString(protocolPayments, "state_file"), "runtime/payment_link_runs.jsonl"));
            AddConfigField(networkForm, fields, row++, "协议超时秒", "protocol_timeout_seconds", FirstNonEmpty(GetString(protocolPayments, "timeout_seconds"), "900"));
            AddConfigSectionHeader(networkForm, row++, "PayPal 与 GoPay", "订单地区、生成模式和本地服务");
            AddConfigField(networkForm, fields, row++, "PayPal代理", "paypal_proxy", FirstListValue(paypal, "proxies"));
            AddConfigComboField(networkForm, comboFields, row++, "订单生成地区", "paypal_billing_region", GetBillingRegionCode(paypal), BillingRegionOptions, "DE");
            AddConfigComboField(networkForm, comboFields, row++, "PayPal直链生成模式", "paypal_link_generation_type", GetLinkGenerationType(paypal), LinkGenerationTypeOptions, "hosted_long_url");
            AddConfigField(networkForm, fields, row++, "GoPay服务地址", "protocol_gopay_service_addr", FirstNonEmpty(GetString(gopay, "payment_service_addr"), "127.0.0.1:50051"));
            AddConfigSectionHeader(networkForm, row++, "地区支付代理", "每种协议独立的代理 Seed");
            AddConfigField(networkForm, fields, row++, "iDEAL", "protocol_ideal_proxy", GetString(idealProtocol, "proxy"));
            AddConfigField(networkForm, fields, row++, "PIX", "protocol_pix_proxy", GetString(pixProtocol, "proxy"));
            AddConfigField(networkForm, fields, row++, "Kakao Pay", "protocol_kakao_proxy", GetString(kakaoProtocol, "proxy"));
            AddConfigField(networkForm, fields, row++, "BLIK", "protocol_blik_proxy", GetString(blikProtocol, "proxy"));
            AddConfigField(networkForm, fields, row++, "TWINT", "protocol_twint_proxy", GetString(twintProtocol, "proxy"));
            AddConfigField(networkForm, fields, row++, "直卡 Checkout", "protocol_direct_card_proxy", GetString(directCardProtocol, "proxy"));
            AddConfigField(networkForm, fields, row++, "MoMo", "protocol_momo_proxy", GetString(momoProtocol, "proxy"));

            var storageForm = AddConfigCategory(sidebar, host, categories, "数据与文件", "Session 输出目录、SQLite 索引和原始 JSON。");
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
            var openJsonButton = new Button { Content = "打开配置文件", Width = 120 };
            openJsonButton.Click += (_, __) => OpenPath(path);
            var saveButton = new Button { Content = "保存", Width = 72, Style = (Style)FindResource("PrimaryButton") };
            saveButton.Click += (_, __) =>
            {
                email["otp_poll_interval"] = fields["otp_poll_interval"].Text.Trim();
                email["token_file"] = fields["token_file"].Text.Trim();
                remail["enabled"] = ConfigBoolValue(fields, "remail_enabled", true);
                remail["base_url"] = fields["remail_base_url"].Text.Trim();
                remail["api_key"] = fields["remail_api_key"].Text.Trim();
                remail["project_id"] = ConfigIntegerValue(fields, "remail_project_id");
                remail["product_id"] = ConfigIntegerValue(fields, "remail_product_id");
                remail["service_mode"] = "code";
                remail["supply"] = fields["remail_supply"].Text.Trim();
                remail["email_suffix"] = fields["remail_email_suffix"].Text.Trim();
                email["remail"] = remail;
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
                blikProtocol.Remove("blik_code");
                twintProtocol["proxy"] = fields["protocol_twint_proxy"].Text.Trim();
                directCardProtocol["proxy"] = fields["protocol_direct_card_proxy"].Text.Trim();
                momoProtocol["proxy"] = fields["protocol_momo_proxy"].Text.Trim();
                protocolMethods["ideal"] = idealProtocol;
                protocolMethods["pix"] = pixProtocol;
                protocolMethods["kakao"] = kakaoProtocol;
                protocolMethods["blik"] = blikProtocol;
                protocolMethods["twint"] = twintProtocol;
                protocolMethods["direct_card"] = directCardProtocol;
                protocolMethods["momo"] = momoProtocol;
                protocolPayments["methods"] = protocolMethods;
                output["directory"] = fields["output_directory"].Text.Trim();
                storage["sqlite_path"] = fields["sqlite_path"].Text.Trim();
                cpaMode["api_url"] = fields["cpa_api_url"].Text.Trim();
                cpaMode["api_token"] = fields["cpa_api_token"].Text.Trim();
                sub2api["api_url"] = fields["sub2api_url"].Text.Trim();
                sub2api["api_token"] = fields["sub2api_token"].Text.Trim();
                agentIdentity["register_on_free_signup"] = ConfigBoolValue(fields, "agent_identity_register_on_free_signup", GetBool(agentIdentity, "register_on_free_signup", false));
                agentIdentity["registration_timeout"] = ConfigIntegerValue(fields, "agent_identity_registration_timeout");
                sub2api["email"] = fields["sub2api_email"].Text.Trim();
                sub2api["password"] = fields["sub2api_password"].Text.Trim();
                sub2api["group_name"] = fields["sub2api_group"].Text.Trim();
                sub2api["group_ids"] = fields["sub2api_group_ids"].Text.Trim();
                sub2api["proxy_name"] = fields["sub2api_proxy"].Text.Trim();
                sub2api["proxy_id"] = fields["sub2api_proxy_id"].Text.Trim();
                sub2api["priority"] = fields["sub2api_priority"].Text.Trim();
                sub2api["concurrency"] = fields["sub2api_concurrency"].Text.Trim();
                sub2api["auth_mode"] = ConfigComboValue(comboFields, "sub2api_auth_mode", "auto");
                sub2api["verify_after_import"] = fields["sub2api_verify_after_import"].Text.Trim();
                string savedRegistrationProxy = fields["registration_proxy"].Text.Trim();
                string savedMailboxProxy = FirstNonEmpty(fields["mailbox_proxy"].Text.Trim(), LocalNonPaymentProxy);
                List<object> savedRegistrationPool = ParseStringList(fields["registration_proxy_pool"].Text);
                if (savedRegistrationProxy.Length > 0)
                {
                    savedRegistrationPool.RemoveAll(item => string.Equals(Convert.ToString(item), savedRegistrationProxy, StringComparison.OrdinalIgnoreCase));
                    savedRegistrationPool.Insert(0, savedRegistrationProxy);
                }
                List<object> savedProtocolPool = ParseStringList(fields["protocol_proxy_pool"].Text);
                proxy["registration"] = savedRegistrationProxy;
                proxy["default"] = savedRegistrationProxy;
                proxy["pool"] = savedRegistrationPool;
                config["mailbox_proxy"] = savedMailboxProxy;
                protocolPayments["proxy_pool"] = savedProtocolPool;
                phoneReuse["proxy"] = savedRegistrationProxy;
                config["email_registration"] = email;
                config["proxy"] = proxy;
                config["paypal"] = paypal;
                config["gopay"] = gopay;
                config["protocol_payments"] = protocolPayments;
                config["output"] = output;
                config["storage"] = storage;
                config["cpa_mode"] = cpaMode;
                config["sub2api"] = sub2api;
                config["agent_identity"] = agentIdentity;
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
                Width = double.NaN,
                Height = 38,
                FontSize = 13,
                FontWeight = FontWeights.SemiBold
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
            button.Click += (_, __) =>
            {
                SelectConfigCategory(categories, category);
                panel.BringIntoView();
            };
            return form;
        }

        private void AddConfigSectionHeader(Grid form, int row, string title, string description)
        {
            form.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var header = new Grid { Margin = new Thickness(0, row == 0 ? 0 : 12, 0, 12) };
            header.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            header.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            header.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            header.Children.Add(new TextBlock
            {
                Text = title,
                FontSize = 14,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 3)
            });
            var detail = new TextBlock
            {
                Text = description,
                FontSize = 11.5,
                Foreground = (Brush)FindResource("TextMuted"),
                TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 0, 0, 8)
            };
            Grid.SetRow(detail, 1);
            header.Children.Add(detail);
            var line = new Border
            {
                Height = 1,
                Background = (Brush)FindResource("Line")
            };
            Grid.SetRow(line, 2);
            header.Children.Add(line);
            Grid.SetRow(header, row);
            Grid.SetColumnSpan(header, 2);
            form.Children.Add(header);
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
                FontSize = 12.5,
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
                IsReadOnly = isReadOnly,
                FontSize = 12.5
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
                FontSize = 12.5,
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
                VerticalContentAlignment = VerticalAlignment.Center,
                FontSize = 12.5
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
                FontSize = 12.5,
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
                VerticalContentAlignment = VerticalAlignment.Center,
                FontSize = 12.5
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
