// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    public sealed record SettingsSectionDefinition(string Title, IReadOnlyList<SettingDefinition> Fields);
    public sealed record SettingsCategoryDefinition(string Title, IReadOnlyList<SettingsSectionDefinition> Sections);

    public static class SettingsCatalog
    {
        /// <summary>
        /// Default SMS provider key. Mirrors `sms_providers.DEFAULT_PROVIDER`
        /// on the Python side; the two are pinned together by
        /// tests/workbench/test_settings_catalog_provider_parity.py.
        /// </summary>
        public const string DefaultPhoneProvider = "smsbower";

        private static SettingDefinition Text(string key, string label, string path, string fallback = "")
            => new(key, label, path, SettingFieldKind.Text, fallback);
        private static SettingDefinition Secret(string key, string label, string path)
            => new(key, label, path, SettingFieldKind.Secret);
        private static SettingDefinition Integer(string key, string label, string path, string fallback = "")
            => new(key, label, path, SettingFieldKind.Number, fallback);
        private static SettingDefinition Boolean(string key, string label, string path, bool fallback)
            => new(key, label, path, SettingFieldKind.Boolean, fallback ? "true" : "false");
        private static SettingDefinition Options(string key, string label, string path, string fallback, params string[] options)
            => new(key, label, path, SettingFieldKind.Options, fallback, options);
        private static SettingDefinition Multiline(string key, string label, string path, string fallback = "")
            => new(key, label, path, SettingFieldKind.Multiline, fallback);
        private static SettingsSectionDefinition Section(string title, params SettingDefinition[] fields) => new(title, fields);
        private static SettingsCategoryDefinition Category(string title, params SettingsSectionDefinition[] sections) => new(title, sections);

        public static IReadOnlyList<SettingsCategoryDefinition> Categories => BuildCategories();

        private static IReadOnlyList<SettingsCategoryDefinition> BuildCategories() => new[]
        {
            Category(Loc.T("邮箱与收信"),
                Section(Loc.T("邮箱池"),
                    Integer("otp_poll_interval", Loc.T("OTP轮询间隔秒"), "email_registration.otp_poll_interval"),
                    Text("token_file", Loc.T("邮箱池文件"), "email_registration.token_file")),
                Section("ReMail",
                    Boolean("remail_enabled", Loc.T("启用"), "email_registration.remail.enabled", true),
                    Text("remail_base_url", Loc.T("API地址"), "email_registration.remail.base_url", "https://remail.aishop6.com"),
                    Secret("remail_api_key", "API Key", "email_registration.remail.api_key"),
                    Integer("remail_project_id", Loc.T("项目ID"), "email_registration.remail.project_id", "2"),
                    Options("remail_supply", Loc.T("库存策略"), "email_registration.remail.supply", "private_first", "private_first", "public_only"),
                    Text("remail_email_suffix", Loc.T("邮箱后缀"), "email_registration.remail.email_suffix", "outlook.com")),
                Section("Smailr",
                    Boolean("smailr_enabled", Loc.T("启用"), "email_registration.smailr.enabled", false),
                    Text("smailr_base_url", Loc.T("API地址"), "email_registration.smailr.base_url", "https://smailr.com"),
                    Secret("smailr_api_key", "API Key", "email_registration.smailr.api_key"),
                    Options("smailr_default_domain", Loc.T("邮箱域名（LV1+）"), "email_registration.smailr.default_domain", "smailr.com",
                        "smailr.com", "loc.cc", "mail.nodeloc.cc", "nodeloc.cc"),
                    Integer("smailr_timeout", Loc.T("请求超时秒数"), "email_registration.smailr.timeout", "30")),
                Section("CFWorker",
                    Text("cfworker_url", "Worker URL", "email_registration.cfworker_url"),
                    Text("cfworker_domain", Loc.T("邮箱域名"), "email_registration.cfworker_domain"),
                    Secret("cfworker_admin_token", "Admin Token", "email_registration.cfworker_admin_token"),
                    Secret("cfworker_api_token", "Cloudflare API Token", "email_registration.cfworker_api_token"))),

            Category(Loc.T("注册与接码"),
                Section(Loc.T("注册驱动"),
                    Options("registration_driver", Loc.T("注册驱动"), "registration.driver", "protocol",
                        "protocol", "playwright", "roxy", "cloak", "camoufox"),
                    Boolean("registration_browser_headless", Loc.T("浏览器无头模式"), "registration.browser_headless", true),
                    Integer("registration_browser_timeout", Loc.T("浏览器超时秒"), "registration.browser_timeout_seconds", "90"),
                    Text("registration_browser_locale", Loc.T("浏览器语言"), "registration.browser_locale", "en-US"),
                    Text("registration_browser_timezone", Loc.T("浏览器时区"), "registration.browser_timezone", "America/New_York")),
                Section("RoxyBrowser",
                    Text("roxy_api_base", Loc.T("本地 API 地址"), "registration.drivers.roxy.api_base", "http://127.0.0.1:50000"),
                    Secret("roxy_api_token", "API Token", "registration.drivers.roxy.api_token"),
                    Text("roxy_workspace_id", Loc.T("工作区 ID"), "registration.drivers.roxy.workspace_id"),
                    Text("roxy_project_id", Loc.T("项目 ID"), "registration.drivers.roxy.project_id"),
                    Text("roxy_profile_id", Loc.T("固定 Profile ID"), "registration.drivers.roxy.profile_id"),
                    Boolean("roxy_delete_profile", Loc.T("任务后删除临时 Profile"), "registration.drivers.roxy.delete_profile_after_run", true),
                    Boolean("roxy_keep_browser_open", Loc.T("保留浏览器现场"), "registration.drivers.roxy.keep_browser_open", false)),
                Section("CloakBrowser",
                    Boolean("cloak_humanize", Loc.T("启用人工化行为"), "registration.drivers.cloak.humanize", true),
                    Boolean("cloak_geoip", Loc.T("按出口匹配环境"), "registration.drivers.cloak.geoip", true),
                    Boolean("cloak_use_proxy", Loc.T("使用注册代理"), "registration.drivers.cloak.use_proxy", true),
                    Secret("cloak_license_key", "License Key", "registration.drivers.cloak.license_key"),
                    Text("cloak_fingerprint_seed", Loc.T("指纹 Seed"), "registration.drivers.cloak.fingerprint_seed"),
                    Text("cloak_user_data_dir", Loc.T("持久化用户目录"), "registration.drivers.cloak.user_data_dir"),
                    Boolean("cloak_keep_browser_open", Loc.T("保留浏览器现场"), "registration.drivers.cloak.keep_browser_open", false)),
                Section("Camoufox",
                    Boolean("camoufox_humanize", Loc.T("启用人工化行为"), "registration.drivers.camoufox.humanize", true),
                    Boolean("camoufox_geoip", Loc.T("按出口匹配环境"), "registration.drivers.camoufox.geoip", true),
                    Boolean("camoufox_use_proxy", Loc.T("使用注册代理"), "registration.drivers.camoufox.use_proxy", true),
                    Integer("camoufox_max_width", Loc.T("屏幕最大宽度"), "registration.drivers.camoufox.max_width", "1280"),
                    Integer("camoufox_max_height", Loc.T("屏幕最大高度"), "registration.drivers.camoufox.max_height", "900"),
                    Text("camoufox_locale", Loc.T("浏览器语言"), "registration.drivers.camoufox.locale"),
                    Text("camoufox_timezone", Loc.T("浏览器时区"), "registration.drivers.camoufox.timezone"),
                    Text("camoufox_user_data_dir", Loc.T("持久化用户目录"), "registration.drivers.camoufox.user_data_dir"),
                    Boolean("camoufox_keep_browser_open", Loc.T("保留浏览器现场"), "registration.drivers.camoufox.keep_browser_open", false)),
                Section(Loc.T("接码供应商"),
                    Options("phone_provider", Loc.T("供应商"), "phone_reuse.source", DefaultPhoneProvider,
                        "smsbower", "herosms", "grizzly", "nexsms"),
                    Secret("phone_provider_api_key", "API Key", "phone_reuse.{provider}.api_key"),
                    Text("phone_provider_endpoint", Loc.T("API 地址（留空用内置默认）"), "phone_reuse.{provider}.endpoint"),
                    Integer("phone_provider_sms_timeout", Loc.T("短信等待秒"), "phone_reuse.{provider}.sms_timeout", "120"),
                    Integer("phone_provider_sms_poll_interval", Loc.T("短信轮询间隔秒"), "phone_reuse.{provider}.sms_poll_interval", "5"),
                    Integer("phone_max_reuse_count", Loc.T("复用次数"), "phone_reuse.max_reuse_count"),
                    Integer("phone_send_cooldown_seconds", Loc.T("发码冷却秒"), "phone_reuse.send_cooldown_seconds"),
                    Integer("phone_send_retry_attempts", Loc.T("发码重试次数"), "phone_reuse.send_retry_attempts"),
                    Integer("phone_send_retry_delay_seconds", Loc.T("发码重试延迟秒"), "phone_reuse.send_retry_delay_seconds"),
                    Text("phone_state_file", Loc.T("状态文件"), "phone_reuse.state_file")),
                Section("Codex OAuth",
                    Integer("codex_registration_timeout", Loc.T("OAuth超时秒"), "codex_oauth.registration_timeout"),
                    Boolean("codex_allow_passwordless_takeover", Loc.T("允许邮箱OTP兜底"), "codex_oauth.allow_passwordless_takeover", false),
                    Boolean("codex_auto_phone_verification", Loc.T("自动手机验证"), "codex_oauth.auto_phone_verification", false),
                    Boolean("codex_require_registration_refresh_token", Loc.T("注册要求RT"), "codex_oauth.require_registration_refresh_token", true),
                    Boolean("codex_require_registration_phone_verification", Loc.T("注册要求手机号"), "codex_oauth.require_registration_phone_verification", true)),
                Section(Loc.T("AT 稳定入库"),
                    Integer("registration_at_stability_probe_count", Loc.T("AT探测次数"), "registration.at_stability_probe_count", "2"),
                    Integer("registration_at_stability_probe_delay", Loc.T("探测间隔秒"), "registration.at_stability_probe_delay_seconds", "10"),
                    Integer("registration_at_probe_timeout", Loc.T("单次探测超时秒"), "registration.at_probe_timeout_seconds", "30")),
                Section(Loc.T("阶段并发"),
                    Integer("registration_auth_concurrency", Loc.T("认证流并发"), "registration.stage_concurrency.auth", "1"),
                    Integer("registration_network_concurrency", Loc.T("注册网络并发"), "registration.stage_concurrency.network", "4"),
                    Integer("registration_at_probe_concurrency", Loc.T("AT探测并发"), "registration.stage_concurrency.at_probe", "4")),
                Section(Loc.T("脉冲调度"),
                    Boolean("pulse_enabled", Loc.T("启用波次调度"), "registration.pulse.enabled", false),
                    Boolean("pulse_canary_enabled", Loc.T("首波与阻断后单账号探针"), "registration.pulse.canary_enabled", true),
                    Integer("pulse_wave_size", Loc.T("单波账号数"), "registration.pulse.wave_size", "4"),
                    Integer("pulse_wave_delay", Loc.T("波间隔秒"), "registration.pulse.wave_delay_seconds", "5"),
                    Integer("pulse_ban_threshold", Loc.T("OTP封禁阈值"), "registration.pulse.ban_threshold", "2"),
                    Integer("pulse_ban_pause", Loc.T("封禁暂停秒"), "registration.pulse.ban_pause_seconds", "60"),
                    Integer("pulse_max_waves", Loc.T("最大波数(0=不限)"), "registration.pulse.max_waves", "0")),
                Section(Loc.T("跨批重试"),
                    Integer("registration_cross_batch_cooldown", Loc.T("跨批冷却秒"), "registration.retry_policy.cross_batch_cooldown_seconds", "1800"),
                    Integer("registration_otp_pending_quarantine", Loc.T("OTP挂起隔离阈值"), "registration.retry_policy.otp_pending_quarantine_threshold", "2")),
                Section(Loc.T("浏览器进程池"),
                    Boolean("browser_pool_enabled", Loc.T("启用进程池"), "registration.browser_process_pool.enabled", false),
                    Integer("browser_pool_max_concurrent", Loc.T("最大并发浏览器"), "registration.browser_process_pool.max_concurrent", "4"),
                    Integer("browser_pool_max_uses", Loc.T("单进程最大复用次数"), "registration.browser_process_pool.max_uses_per_process", "10"),
                    Boolean("browser_pool_recycle_on_error", Loc.T("出错即回收进程"), "registration.browser_process_pool.recycle_on_error", true)),
                Section("Sentinel",
                    Text("sentinel_version", Loc.T("Sentinel版本"), "email_registration.sentinel_version"),
                    Integer("sentinel_max_concurrency", Loc.T("提取并发"), "email_registration.sentinel_max_concurrency", "2"),
                    Integer("sentinel_prewarm_window", Loc.T("一对一预热窗口"), "email_registration.sentinel_prewarm_window", "4"),
                    Integer("sentinel_circuit_failures", Loc.T("熔断失败次数"), "email_registration.sentinel_circuit_failures", "3"),
                    Integer("sentinel_circuit_cooldown", Loc.T("熔断冷却秒"), "email_registration.sentinel_circuit_cooldown_seconds", "60"))),

            Category(Loc.T("导入与账号"),
                Section("CPA",
                    Text("cpa_api_url", Loc.T("CPA地址"), "cpa_mode.api_url"),
                    Secret("cpa_api_token", "CPA Token", "cpa_mode.api_token")),
                Section("SUB2API",
                    Text("sub2api_url", Loc.T("API地址"), "sub2api.api_url"),
                    Secret("sub2api_token", "API Token", "sub2api.api_token"),
                    Text("sub2api_email", Loc.T("登录邮箱"), "sub2api.email"),
                    Secret("sub2api_password", Loc.T("登录密码"), "sub2api.password"),
                    Text("sub2api_group", Loc.T("目标分组"), "sub2api.group_name"),
                    Text("sub2api_group_ids", Loc.T("分组ID"), "sub2api.group_ids"),
                    Text("sub2api_proxy", Loc.T("远端代理"), "sub2api.proxy_name"),
                    Text("sub2api_proxy_id", Loc.T("代理ID"), "sub2api.proxy_id"),
                    Integer("sub2api_priority", Loc.T("优先级"), "sub2api.priority"),
                    Integer("sub2api_concurrency", Loc.T("账号并发"), "sub2api.concurrency"),
                    Options("sub2api_auth_mode", Loc.T("凭据模式"), "sub2api.auth_mode", "auto", "auto", "oauth", "agent_identity"),
                    Boolean("sub2api_verify_after_import", Loc.T("导入后连通测试"), "sub2api.verify_after_import", true))),

            Category(Loc.T("网络与支付"),
                Section(Loc.T("基础网络"),
                    // No prefilled proxy: a machine-specific localhost default in the box
                    // used to be saved as if the operator had typed it. Blank now stays
                    // blank and the backend resolves proxy.default / the lane pools.
                    Text("registration_proxy", Loc.T("注册代理（主，支持 host:port:user:password / http / socks5 / socks5h）"), "", ""),
                    Multiline("registration_proxy_pool", Loc.T("注册代理池（支持 host:port:user:password / http / socks5 / socks5h）"), ""),
                    Text("mailbox_proxy", Loc.T("邮箱收件代理"), "", ""),
                    Multiline("mailbox_proxy_pool", Loc.T("邮箱收件代理池（网络异常时自动切换）"), ""),
                    Text("browser_registration_proxy", Loc.T("浏览器注册代理（主）"), "", ""),
                    Multiline("browser_registration_proxy_pool", Loc.T("浏览器注册代理池（留空跟随注册池）"), ""),
                    Multiline("payment_qualification_proxy_pool", Loc.T("支付资格专用代理池（Stripe 可达）"), "")),
                Section(Loc.T("协议管理"),
                    Text("protocol_enabled_methods", Loc.T("启用方式"), "", "paypal,gopay,gcash,grabpay,upi,ideal,pix,kakao,blik,twint,direct_card,momo"),
                    Text("protocol_reference_root", Loc.T("提链器目录"), "protocol_payments.reference_root", "services/protocol-payment"),
                    Text("protocol_state_file", Loc.T("状态文件"), "protocol_payments.state_file", "runtime/payment_link_runs.jsonl"),
                    Integer("protocol_timeout_seconds", Loc.T("协议超时秒"), "protocol_payments.timeout_seconds", "900")),
                Section(Loc.T("正式批量支付"),
                    Integer("protocol_batch_momo_workers", Loc.T("MoMo并发"), "protocol_payments.batch.method_workers.momo", "2"),
                    Integer("protocol_batch_kakao_workers", Loc.T("Kakao并发"), "protocol_payments.batch.method_workers.kakao", "2"),
                    Integer("protocol_batch_gopay_workers", Loc.T("GoPay并发"), "protocol_payments.batch.method_workers.gopay", "2"),
                    Integer("protocol_batch_gcash_workers", Loc.T("GCash并发"), "protocol_payments.batch.method_workers.gcash", "2"),
                    Integer("protocol_batch_grabpay_workers", Loc.T("GrabPay并发"), "protocol_payments.batch.method_workers.grabpay", "2"),
                    Boolean("protocol_batch_pause_on_canary_failure", Loc.T("Canary失败暂停"), "protocol_payments.batch.pause_on_canary_failure", true),
                    Integer("protocol_batch_canary_pause_seconds", Loc.T("暂停秒数"), "protocol_payments.batch.canary_pause_seconds", "21600"),
                    Multiline("protocol_payment_matrix", Loc.T("地区资格矩阵 JSON"), "")),
                Section("PayPal",
                    Multiline("paypal_proxy", Loc.T("PayPal代理池"), ""),
                    Options("paypal_billing_region", Loc.T("订单生成地区"), "", "DE", "JP", "US", "AU", "DE", "FR", "GB", "IN", "BR"),
                    Options("paypal_link_generation_type", Loc.T("PayPal直链生成模式"), "paypal.link_generation_type", "hosted_long_url", "hosted_long_url", "paypal_direct", "paypal_direct_zero_due"))),

            Category(Loc.T("数据与文件"),
                Section(Loc.T("运行环境"),
                    Text("python_path", Loc.T("Python解释器路径"), "runtime.python_path", "python")),
                Section(Loc.T("本地存储"),
                    Text("output_directory", Loc.T("Session目录"), "output.directory"),
                    Text("sqlite_path", Loc.T("SQLite路径"), "storage.sqlite_path")))
        };

        public static IEnumerable<SettingDefinition> AllFields => Categories.SelectMany(category => category.Sections).SelectMany(section => section.Fields);
    }
}
