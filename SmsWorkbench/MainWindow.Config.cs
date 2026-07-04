using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Globalization;
using System.Windows.Data;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using FluentWindow = Wpf.Ui.Controls.FluentWindow;

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
            var paypalBrowser = GetSection(config, "paypal_browser");
            var paypalNocard = GetSection(config, "paypal_nocard");
            var gopay = GetSection(config, "gopay");
            var gopayStageProxies = GetChildSection(gopay, "stage_proxies");
            var gopayWaRebind = GetChildSection(gopay, "wa_rebind");
            var gopayOtp = GetChildSection(gopay, "otp");
            var gopayOtpSmsBower = GetChildSection(gopayOtp, "smsbower");
            var storage = GetSection(config, "storage");
            var output = GetSection(config, "output");
            var cpaMode = GetSection(config, "cpa_mode");
            var sub2api = GetSection(config, "sub2api");
            var codexOauth = GetSection(config, "codex_oauth");
            var phoneReuse = GetSection(config, "phone_reuse");
            var smsBower = GetChildSection(phoneReuse, "smsbower");
            var nextSms = GetChildSection(phoneReuse, "nextsms");

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

            var phoneForm = AddConfigCategory(sidebar, host, categories, "手机接码", "SMSBower / NextSMS 手机号接码、复用次数和 Codex OAuth 接码开关。");
            row = 0;
            AddConfigComboField(phoneForm, comboFields, row++, "接码来源", "phone_source", FirstNonEmpty(GetString(phoneReuse, "source"), "smsbower"), new[] { "smsbower", "nextsms", "phone_pool" });
            AddConfigField(phoneForm, fields, row++, "SMSBower API Key", "smsbower_api_key", GetString(smsBower, "api_key"));
            AddConfigField(phoneForm, fields, row++, "服务代码", "smsbower_service", GetString(smsBower, "service"));
            AddConfigComboField(phoneForm, comboFields, row++, "国家代码", "smsbower_country", GetString(smsBower, "country"), SmsBowerCountryOptions, "38");
            AddConfigField(phoneForm, fields, row++, "NextSMS API Key", "nextsms_api_key", GetString(nextSms, "api_key"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Endpoint", "nextsms_endpoint", FirstNonEmpty(GetString(nextSms, "endpoint"), "https://sms.nextactionplus.com/api/"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Service", "nextsms_service", FirstNonEmpty(GetString(nextSms, "service"), "openai"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Country", "nextsms_country", FirstNonEmpty(GetString(nextSms, "country"), "US"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Pricing", "nextsms_pricing_option", FirstNonEmpty(GetString(nextSms, "pricing_option"), "0"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Pool Size", "nextsms_pool_size", FirstNonEmpty(GetString(nextSms, "pool_size"), "1"));
            AddConfigField(phoneForm, fields, row++, "NextSMS SMS Timeout", "nextsms_sms_timeout", FirstNonEmpty(GetString(nextSms, "sms_timeout"), "120"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Poll Interval", "nextsms_sms_poll_interval", FirstNonEmpty(GetString(nextSms, "sms_poll_interval"), "5"));
            AddConfigField(phoneForm, fields, row++, "NextSMS Number Attempts", "nextsms_number_attempts", FirstNonEmpty(GetString(nextSms, "number_attempts"), "3"));
            AddConfigField(phoneForm, fields, row++, "GoPay SMSBower服务代码", "smsbower_gopay_service", GetString(smsBower, "gopay_service"));
            AddConfigField(phoneForm, fields, row++, "GoPay SMSBower国家代码", "smsbower_gopay_country", GetString(smsBower, "gopay_country"));
            AddConfigField(phoneForm, fields, row++, "GoPay SMSBower最低价格", "smsbower_gopay_min_price", GetString(smsBower, "gopay_min_price"));
            AddConfigField(phoneForm, fields, row++, "GoPay SMSBower最高价格", "smsbower_gopay_max_price", GetString(smsBower, "gopay_max_price"));
            AddConfigField(phoneForm, fields, row++, "最低价格", "smsbower_min_price", GetString(smsBower, "min_price"));
            AddConfigField(phoneForm, fields, row++, "最高价格", "smsbower_max_price", GetString(smsBower, "max_price"));
            AddConfigField(phoneForm, fields, row++, "目标价格", "smsbower_target_price", GetString(smsBower, "target_price"));
            AddConfigField(phoneForm, fields, row++, "号码池数量", "smsbower_pool_size", GetString(smsBower, "pool_size"));
            AddConfigField(phoneForm, fields, row++, "短信等待秒", "smsbower_sms_timeout", GetString(smsBower, "sms_timeout"));
            AddConfigField(phoneForm, fields, row++, "短信轮询间隔秒", "smsbower_sms_poll_interval", GetString(smsBower, "sms_poll_interval"));
            AddConfigField(phoneForm, fields, row++, "复用次数", "phone_max_reuse_count", GetString(phoneReuse, "max_reuse_count"));
            AddConfigField(phoneForm, fields, row++, "发码冷却秒", "phone_send_cooldown_seconds", GetString(phoneReuse, "send_cooldown_seconds"));
            AddConfigField(phoneForm, fields, row++, "发码重试次数", "phone_send_retry_attempts", GetString(phoneReuse, "send_retry_attempts"));
            AddConfigField(phoneForm, fields, row++, "发码重试延迟秒", "phone_send_retry_delay_seconds", GetString(phoneReuse, "send_retry_delay_seconds"));
            AddConfigField(phoneForm, fields, row++, "状态文件", "phone_state_file", GetString(phoneReuse, "state_file"));
            AddConfigField(phoneForm, fields, row++, "固定号码池", "phone_pool_lines", FormatPhonePool(phoneReuse), multiline: true);
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

            var proxyForm = AddConfigCategory(sidebar, host, categories, "代理 / 支付", "默认代理、PayPal 链接生成代理和直链模式。");
            row = 0;
            AddConfigField(proxyForm, fields, row++, "默认代理", "default_proxy", GetString(proxy, "default"));
            AddConfigField(proxyForm, fields, row++, "PayPal代理", "paypal_proxy", FirstListValue(paypal, "proxies"));
            AddConfigComboField(proxyForm, comboFields, row++, "订单生成地区", "paypal_billing_region", GetBillingRegionCode(paypal), BillingRegionOptions, "DE");
            AddConfigComboField(proxyForm, comboFields, row++, "PayPal直链生成模式", "paypal_link_generation_type", GetLinkGenerationType(paypal), LinkGenerationTypeOptions, "hosted_long_url");

            var paypalBrowserForm = AddConfigCategory(sidebar, host, categories, "PayPal浏览器", "项目内置浏览器支付、身份生成和接码号码池。");
            row = 0;
            AddConfigField(paypalBrowserForm, fields, row++, "启用", "paypal_browser_enabled", FirstNonEmpty(GetString(paypalBrowser, "enabled"), "true"));
            AddConfigField(paypalBrowserForm, fields, row++, "浏览器引擎", "paypal_browser_browser_engine", FirstNonEmpty(GetString(paypalBrowser, "browser_engine"), "camoufox"));
            AddConfigField(paypalBrowserForm, fields, row++, "身份国家", "paypal_browser_country", FirstNonEmpty(GetString(paypalBrowser, "country"), "US"));
            AddConfigField(paypalBrowserForm, fields, row++, "无头模式", "paypal_browser_headless", FirstNonEmpty(GetString(paypalBrowser, "headless"), "true"));
            AddConfigField(paypalBrowserForm, fields, row++, "允许人工人机验证", "paypal_browser_manual_human_verification", FirstNonEmpty(GetString(paypalBrowser, "manual_human_verification"), "false"));
            AddConfigField(paypalBrowserForm, fields, row++, "人机验证等待秒", "paypal_browser_human_verification_timeout", FirstNonEmpty(GetString(paypalBrowser, "human_verification_timeout"), "300"));
            AddConfigField(paypalBrowserForm, fields, row++, "支付邮箱模式", "paypal_browser_email_mode", FirstNonEmpty(GetString(paypalBrowser, "email_mode"), "random"));
            AddConfigField(paypalBrowserForm, fields, row++, "接码号码池", "paypal_browser_phone_pool", FormatPhonePool(paypalBrowser, paypalNocard), multiline: true);

            var gopayForm = AddConfigCategory(sidebar, host, categories, "GoPay", "GoPay 生链、协议支付服务和分阶段代理配置。");
            row = 0;
            AddConfigField(gopayForm, fields, row++, "一键支付模式", "gopay_one_click_mode", FirstNonEmpty(GetString(gopay, "one_click_mode"), "protocol"));
            AddConfigField(gopayForm, fields, row++, "自动打开链接", "gopay_open_link", FirstNonEmpty(GetString(gopay, "open_link"), "true"));
            AddConfigField(gopayForm, fields, row++, "自动生成链接", "gopay_auto_generate", FirstNonEmpty(GetString(gopay, "auto_generate"), "true"));
            AddConfigField(gopayForm, fields, row++, "Provider接口", "gopay_provider_api", FirstNonEmpty(GetString(gopay, "provider_api"), "byte-v-forge"));
            AddConfigField(gopayForm, fields, row++, "PaymentService地址", "gopay_payment_service_addr", FirstNonEmpty(GetString(gopay, "payment_service_addr"), "127.0.0.1:50051"));
            AddConfigField(gopayForm, fields, row++, "grpcurl路径", "gopay_grpcurl_path", FirstNonEmpty(GetString(gopay, "grpcurl_path"), "grpcurl"));
            AddConfigField(gopayForm, fields, row++, "gRPC服务名", "gopay_payment_service", FirstNonEmpty(GetString(gopay, "payment_service"), "payment.PaymentService"));
            AddConfigField(gopayForm, fields, row++, "Proto目录", "gopay_proto_import_path", FirstNonEmpty(GetString(gopay, "proto_import_path"), "services\\gopay-flow\\proto"));
            AddConfigField(gopayForm, fields, row++, "Proto文件", "gopay_proto_path", FirstNonEmpty(GetString(gopay, "proto_path"), "services\\gopay-flow\\proto\\payment.proto"));
            AddConfigField(gopayForm, fields, row++, "Provider超时秒", "gopay_provider_timeout_seconds", FirstNonEmpty(GetString(gopay, "provider_timeout_seconds"), "600"));
            AddConfigField(gopayForm, fields, row++, "服务配置模板", "gopay_provider_config_path", FirstNonEmpty(GetString(gopay, "provider_config_path"), "services\\gopay-flow\\config.gopay.base.json"));
            AddConfigField(gopayForm, fields, row++, "Tokenization", "gopay_tokenization", FirstNonEmpty(GetString(gopay, "tokenization"), "qris"));
            AddConfigField(gopayForm, fields, row++, "GoPay手机号", "gopay_phone", FirstNonEmpty(GetString(gopay, "phone"), GetString(gopay, "phone_number")));
            AddConfigField(gopayForm, fields, row++, "国家区号", "gopay_country_code", FirstNonEmpty(GetString(gopay, "country_code"), "62"));
            AddConfigField(gopayForm, fields, row++, "OTP渠道", "gopay_otp_channel", FirstNonEmpty(GetString(gopay, "otp_channel"), "sms"));
            AddConfigField(gopayForm, fields, row++, "OTP来源", "gopay_otp_source", FirstNonEmpty(GetString(gopay, "otp_source"), FirstNonEmpty(GetString(gopayOtp, "source"), "smsbower")));
            AddConfigField(gopayForm, fields, row++, "GoPay SMSBower服务代码", "gopay_smsbower_service", GetString(gopayOtpSmsBower, "service"));
            AddConfigField(gopayForm, fields, row++, "GoPay SMSBower国家代码", "gopay_smsbower_country", GetString(gopayOtpSmsBower, "country"));
            AddConfigField(gopayForm, fields, row++, "GoPay SMSBower最低价格", "gopay_smsbower_min_price", FirstNonEmpty(GetString(gopayOtpSmsBower, "min_price"), GetString(smsBower, "gopay_min_price")));
            AddConfigField(gopayForm, fields, row++, "GoPay SMSBower最高价格", "gopay_smsbower_max_price", FirstNonEmpty(GetString(gopayOtpSmsBower, "max_price"), GetString(smsBower, "gopay_max_price")));
            AddConfigField(gopayForm, fields, row++, "GoPay PIN", "gopay_pin", GetString(gopay, "pin"));
            AddConfigField(gopayForm, fields, row++, "人工确认后自动确认", "gopay_confirm_after_manual", FirstNonEmpty(GetString(gopay, "confirm_after_manual"), "false"));
            AddConfigField(gopayForm, fields, row++, "MuMu主程序", "gopay_emulator_exe", FirstNonEmpty(GetString(gopay, "emulator_exe"), "D:\\Program Files\\Netease\\MuMuPlayer\\nx_main\\MuMuNxMain.exe"));
            AddConfigField(gopayForm, fields, row++, "ADB路径", "gopay_adb_path", FirstNonEmpty(GetString(gopay, "adb_path"), "D:\\Program Files\\Netease\\MuMuPlayer\\nx_main\\adb.exe"));
            AddConfigField(gopayForm, fields, row++, "ADB Serial", "gopay_adb_serial", FirstNonEmpty(GetString(gopay, "adb_serial"), "emulator-5554"));
            AddConfigField(gopayForm, fields, row++, "ADB Sidecar", "gopay_adb_sidecar_addr", FirstNonEmpty(GetString(gopay, "adb_sidecar_addr"), "127.0.0.1:9999"));
            AddConfigField(gopayForm, fields, row++, "WA换绑启用", "gopay_wa_enabled", FirstNonEmpty(GetString(gopayWaRebind, "enabled"), "false"));
            AddConfigField(gopayForm, fields, row++, "WA支付后换绑", "gopay_wa_rebind_after_payment", FirstNonEmpty(GetString(gopayWaRebind, "rebind_after_payment"), "true"));
            AddConfigField(gopayForm, fields, row++, "GoPay App服务", "gopay_wa_app_service_addr", FirstNonEmpty(GetString(gopayWaRebind, "gopay_app_service_addr"), "127.0.0.1:50060"));
            AddConfigField(gopayForm, fields, row++, "GoPay App Proto目录", "gopay_wa_app_proto_import_path", FirstNonEmpty(GetString(gopayWaRebind, "gopay_app_proto_import_path"), "services\\gopay-app\\proto"));
            AddConfigField(gopayForm, fields, row++, "GoPay App Proto文件", "gopay_wa_app_proto_path", FirstNonEmpty(GetString(gopayWaRebind, "gopay_app_proto_path"), "services\\gopay-app\\proto\\gopay_app.proto"));
            AddConfigField(gopayForm, fields, row++, "WA UserId", "gopay_wa_user_id", FirstNonEmpty(GetString(gopayWaRebind, "user_id"), "local"));
            AddConfigField(gopayForm, fields, row++, "WA支付手机号", "gopay_wa_phone", GetString(gopayWaRebind, "wa_phone"));
            AddConfigField(gopayForm, fields, row++, "换绑目标手机号", "gopay_wa_rebind_phone", GetString(gopayWaRebind, "rebind_phone"));
            AddConfigField(gopayForm, fields, row++, "Checkout代理", "gopay_proxy_checkout", GetString(gopayStageProxies, "checkout"));
            AddConfigField(gopayForm, fields, row++, "Stripe Init代理", "gopay_proxy_stripe_init", GetString(gopayStageProxies, "stripe_init"));
            AddConfigField(gopayForm, fields, row++, "PM Create代理", "gopay_proxy_payment_method", GetString(gopayStageProxies, "payment_method"));
            AddConfigField(gopayForm, fields, row++, "Confirm代理", "gopay_proxy_confirm", GetString(gopayStageProxies, "confirm"));

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
                smsBower["service"] = fields["smsbower_service"].Text.Trim();
                var smsBowerCountry = ConfigComboOptionValue(comboFields, "smsbower_country", "38");
                smsBower["country"] = smsBowerCountry.Value;
                smsBower["country_name"] = smsBowerCountry.Metadata;
                smsBower["country_prefix"] = smsBowerCountry.Extra;
                smsBower["gopay_service"] = fields["smsbower_gopay_service"].Text.Trim();
                smsBower["gopay_country"] = fields["smsbower_gopay_country"].Text.Trim();
                smsBower["gopay_min_price"] = fields["smsbower_gopay_min_price"].Text.Trim();
                smsBower["gopay_max_price"] = fields["smsbower_gopay_max_price"].Text.Trim();
                smsBower["min_price"] = fields["smsbower_min_price"].Text.Trim();
                smsBower["max_price"] = fields["smsbower_max_price"].Text.Trim();
                smsBower["target_price"] = fields["smsbower_target_price"].Text.Trim();
                smsBower["pool_size"] = ConfigIntegerValue(fields, "smsbower_pool_size");
                smsBower["sms_timeout"] = ConfigIntegerValue(fields, "smsbower_sms_timeout");
                smsBower["sms_poll_interval"] = ConfigIntegerValue(fields, "smsbower_sms_poll_interval");
                phoneReuse["source"] = ConfigComboValue(comboFields, "phone_source", "smsbower");
                phoneReuse["smsbower"] = smsBower;
                nextSms["api_key"] = fields["nextsms_api_key"].Text.Trim();
                nextSms["endpoint"] = fields["nextsms_endpoint"].Text.Trim();
                nextSms["service"] = fields["nextsms_service"].Text.Trim();
                nextSms["country"] = fields["nextsms_country"].Text.Trim();
                nextSms["pricing_option"] = ConfigIntegerValue(fields, "nextsms_pricing_option");
                nextSms["pool_size"] = ConfigIntegerValue(fields, "nextsms_pool_size");
                nextSms["sms_timeout"] = ConfigIntegerValue(fields, "nextsms_sms_timeout");
                nextSms["sms_poll_interval"] = ConfigIntegerValue(fields, "nextsms_sms_poll_interval");
                nextSms["number_attempts"] = ConfigIntegerValue(fields, "nextsms_number_attempts");
                phoneReuse["nextsms"] = nextSms;
                phoneReuse["max_reuse_count"] = ConfigIntegerValue(fields, "phone_max_reuse_count");
                phoneReuse["send_cooldown_seconds"] = ConfigIntegerValue(fields, "phone_send_cooldown_seconds");
                phoneReuse["send_retry_attempts"] = ConfigIntegerValue(fields, "phone_send_retry_attempts");
                phoneReuse["send_retry_delay_seconds"] = ConfigIntegerValue(fields, "phone_send_retry_delay_seconds");
                phoneReuse["state_file"] = fields["phone_state_file"].Text.Trim();
                phoneReuse["phone_pool"] = ParsePhonePoolLines(fields["phone_pool_lines"].Text);
                codexOauth["registration_timeout"] = ConfigIntegerValue(fields, "codex_registration_timeout");
                codexOauth["allow_passwordless_takeover"] = ConfigBoolValue(fields, "codex_allow_passwordless_takeover", GetBool(codexOauth, "allow_passwordless_takeover", false));
                codexOauth["auto_phone_verification"] = ConfigBoolValue(fields, "codex_auto_phone_verification", GetBool(codexOauth, "auto_phone_verification", false));
                codexOauth["require_registration_refresh_token"] = ConfigBoolValue(fields, "codex_require_registration_refresh_token", GetBool(codexOauth, "require_registration_refresh_token", true));
                codexOauth["require_registration_phone_verification"] = ConfigBoolValue(fields, "codex_require_registration_phone_verification", GetBool(codexOauth, "require_registration_phone_verification", true));
                proxy["default"] = fields["default_proxy"].Text.Trim();
                paypal["proxies"] = new List<object> { fields["paypal_proxy"].Text.Trim() };
                paypal["billing_regions"] = new List<object> { ConfigComboOptionValue(comboFields, "paypal_billing_region", "DE").Value };
                paypal["link_generation_type"] = ConfigComboOptionValue(comboFields, "paypal_link_generation_type", "hosted_long_url").Value;
                paypalBrowser["enabled"] = ConfigBoolValue(fields, "paypal_browser_enabled", GetBool(paypalBrowser, "enabled", true));
                paypalBrowser.Remove("pp_auto_path");
                paypalBrowser.Remove("engine");
                paypalBrowser.Remove("firefox_path");
                paypalBrowser["browser_engine"] = fields["paypal_browser_browser_engine"].Text.Trim();
                paypalBrowser["country"] = fields["paypal_browser_country"].Text.Trim();
                paypalBrowser["headless"] = ConfigBoolValue(fields, "paypal_browser_headless", GetBool(paypalBrowser, "headless", true));
                paypalBrowser["manual_human_verification"] = ConfigBoolValue(fields, "paypal_browser_manual_human_verification", GetBool(paypalBrowser, "manual_human_verification", false));
                paypalBrowser["human_verification_timeout"] = ConfigIntegerValue(fields, "paypal_browser_human_verification_timeout");
                paypalBrowser["email_mode"] = fields["paypal_browser_email_mode"].Text.Trim();
                paypalBrowser["phone_pool"] = ParsePhonePoolLines(fields["paypal_browser_phone_pool"].Text);
                gopay["one_click_mode"] = fields["gopay_one_click_mode"].Text.Trim();
                gopay["open_link"] = ConfigBoolValue(fields, "gopay_open_link", GetBool(gopay, "open_link", true));
                gopay["auto_generate"] = ConfigBoolValue(fields, "gopay_auto_generate", GetBool(gopay, "auto_generate", true));
                gopay["provider_api"] = fields["gopay_provider_api"].Text.Trim();
                gopay["payment_service_addr"] = fields["gopay_payment_service_addr"].Text.Trim();
                gopay["grpcurl_path"] = fields["gopay_grpcurl_path"].Text.Trim();
                gopay["payment_service"] = fields["gopay_payment_service"].Text.Trim();
                gopay["proto_import_path"] = fields["gopay_proto_import_path"].Text.Trim();
                gopay["proto_path"] = fields["gopay_proto_path"].Text.Trim();
                gopay["provider_timeout_seconds"] = ConfigIntegerValue(fields, "gopay_provider_timeout_seconds");
                gopay["provider_config_path"] = fields["gopay_provider_config_path"].Text.Trim();
                gopay["tokenization"] = fields["gopay_tokenization"].Text.Trim();
                gopay["phone"] = fields["gopay_phone"].Text.Trim();
                gopay["country_code"] = fields["gopay_country_code"].Text.Trim();
                gopay["otp_channel"] = fields["gopay_otp_channel"].Text.Trim();
                gopay["otp_source"] = fields["gopay_otp_source"].Text.Trim();
                gopayOtp["source"] = fields["gopay_otp_source"].Text.Trim();
                gopayOtpSmsBower["api_key"] = fields["smsbower_api_key"].Text.Trim();
                gopayOtpSmsBower["endpoint"] = GetString(smsBower, "endpoint");
                gopayOtpSmsBower["service"] = fields["gopay_smsbower_service"].Text.Trim();
                gopayOtpSmsBower["country"] = fields["gopay_smsbower_country"].Text.Trim();
                gopayOtpSmsBower["min_price"] = fields["gopay_smsbower_min_price"].Text.Trim();
                gopayOtpSmsBower["max_price"] = fields["gopay_smsbower_max_price"].Text.Trim();
                gopayOtpSmsBower["sms_timeout"] = ConfigIntegerValue(fields, "smsbower_sms_timeout");
                gopayOtpSmsBower["sms_poll_interval"] = ConfigIntegerValue(fields, "smsbower_sms_poll_interval");
                gopayOtp["smsbower"] = gopayOtpSmsBower;
                gopay["otp"] = gopayOtp;
                gopay["pin"] = fields["gopay_pin"].Text.Trim();
                gopay["confirm_after_manual"] = ConfigBoolValue(fields, "gopay_confirm_after_manual", GetBool(gopay, "confirm_after_manual", false));
                gopay["emulator_exe"] = fields["gopay_emulator_exe"].Text.Trim();
                gopay["adb_path"] = fields["gopay_adb_path"].Text.Trim();
                gopay["adb_serial"] = fields["gopay_adb_serial"].Text.Trim();
                gopay["adb_sidecar_addr"] = fields["gopay_adb_sidecar_addr"].Text.Trim();
                gopayWaRebind["enabled"] = ConfigBoolValue(fields, "gopay_wa_enabled", GetBool(gopayWaRebind, "enabled", false));
                gopayWaRebind["rebind_after_payment"] = ConfigBoolValue(fields, "gopay_wa_rebind_after_payment", GetBool(gopayWaRebind, "rebind_after_payment", true));
                gopayWaRebind["gopay_app_service_addr"] = fields["gopay_wa_app_service_addr"].Text.Trim();
                gopayWaRebind["gopay_app_service"] = FirstNonEmpty(GetString(gopayWaRebind, "gopay_app_service"), "gopay_app.GopayAppService");
                gopayWaRebind["gopay_app_proto_import_path"] = fields["gopay_wa_app_proto_import_path"].Text.Trim();
                gopayWaRebind["gopay_app_proto_path"] = fields["gopay_wa_app_proto_path"].Text.Trim();
                gopayWaRebind["user_id"] = fields["gopay_wa_user_id"].Text.Trim();
                gopayWaRebind["wa_phone"] = fields["gopay_wa_phone"].Text.Trim();
                gopayWaRebind["rebind_phone"] = fields["gopay_wa_rebind_phone"].Text.Trim();
                gopayWaRebind["timeout_seconds"] = ConfigIntegerValue(fields, "gopay_provider_timeout_seconds");
                gopay["wa_rebind"] = gopayWaRebind;
                gopay["billing_regions"] = new List<object> { "ID" };
                gopayStageProxies["checkout"] = fields["gopay_proxy_checkout"].Text.Trim();
                gopayStageProxies["stripe_init"] = fields["gopay_proxy_stripe_init"].Text.Trim();
                gopayStageProxies["payment_method"] = fields["gopay_proxy_payment_method"].Text.Trim();
                gopayStageProxies["confirm"] = fields["gopay_proxy_confirm"].Text.Trim();
                gopay["stage_proxies"] = gopayStageProxies;
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
                config["email_registration"] = email;
                config["proxy"] = proxy;
                config["paypal"] = paypal;
                config["paypal_browser"] = paypalBrowser;
                config["gopay"] = gopay;
                config["output"] = output;
                config["storage"] = storage;
                config["cpa_mode"] = cpaMode;
                config["sub2api"] = sub2api;
                config["codex_oauth"] = codexOauth;
                config["phone_reuse"] = phoneReuse;
                SaveConfig(path, config);
                ProxyText = fields["default_proxy"].Text.Trim();
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

        private void AddConfigField(Grid form, Dictionary<string, TextBox> fields, int row, string label, string key, string value, bool multiline = false)
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
                VerticalContentAlignment = multiline ? VerticalAlignment.Top : VerticalAlignment.Center
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
            if (fields.TryGetValue(key, out ComboBox combo) && combo.SelectedItem is ConfigComboOption selected)
            {
                return selected;
            }
            return SmsBowerCountryOptions.FirstOrDefault(option => option.Value.Equals(fallback, StringComparison.OrdinalIgnoreCase))
                ?? SmsBowerCountryOptions.First();
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

        private string FormatPhonePool(Dictionary<string, object> phoneReuse)
        {
            if (!phoneReuse.TryGetValue("phone_pool", out object value) || value is not List<object> list)
            {
                return "";
            }
            var lines = new List<string>();
            foreach (object item in list)
            {
                if (item is not Dictionary<string, object> entry) continue;
                string phone = GetString(entry, "phone").Trim();
                string smsApiUrl = GetString(entry, "sms_api_url").Trim();
                if (phone.Length == 0 || smsApiUrl.Length == 0) continue;
                lines.Add(phone + "----" + smsApiUrl);
            }
            return string.Join(Environment.NewLine, lines);
        }

        private string FormatPhonePool(Dictionary<string, object> primary, Dictionary<string, object> fallback)
        {
            string value = FormatPhonePool(primary);
            return value.Length > 0 ? value : FormatPhonePool(fallback);
        }

        private List<object> ParsePhonePoolLines(string raw)
        {
            var items = new List<object>();
            foreach (string sourceLine in (raw ?? "").Split(new[] { "\r\n", "\n" }, StringSplitOptions.None))
            {
                string line = sourceLine.Trim();
                if (line.Length == 0) continue;
                string phone = "";
                string smsApiUrl = "";
                int marker = line.IndexOf("----", StringComparison.Ordinal);
                if (marker >= 0)
                {
                    phone = line.Substring(0, marker).Trim();
                    smsApiUrl = line.Substring(marker + 4).Trim();
                }
                else
                {
                    Match match = Regex.Match(line, @"^(\+\d+)\s+(\S+)$");
                    if (match.Success)
                    {
                        phone = match.Groups[1].Value.Trim();
                        smsApiUrl = match.Groups[2].Value.Trim();
                    }
                }
                if (phone.Length == 0 || smsApiUrl.Length == 0) continue;
                items.Add(new Dictionary<string, object>
                {
                    ["phone"] = phone,
                    ["sms_api_url"] = smsApiUrl,
                    ["provider"] = "legacy"
                });
            }
            return items;
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
