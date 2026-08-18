namespace SmsWorkbench
{
    public sealed record SettingsSectionDefinition(string Title, IReadOnlyList<SettingDefinition> Fields);
    public sealed record SettingsCategoryDefinition(string Title, IReadOnlyList<SettingsSectionDefinition> Sections);

    public static class SettingsCatalog
    {
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

        public static IReadOnlyList<SettingsCategoryDefinition> Categories { get; } = new[]
        {
            Category("邮箱与收信",
                Section("邮箱池",
                    Integer("otp_poll_interval", "OTP轮询间隔秒", "email_registration.otp_poll_interval"),
                    Text("token_file", "邮箱池文件", "email_registration.token_file")),
                Section("ReMail",
                    Boolean("remail_enabled", "启用", "email_registration.remail.enabled", true),
                    Text("remail_base_url", "API地址", "email_registration.remail.base_url", "https://remail.aishop6.com"),
                    Secret("remail_api_key", "API Key", "email_registration.remail.api_key"),
                    Integer("remail_project_id", "项目ID", "email_registration.remail.project_id", "2"),
                    Options("remail_supply", "库存策略", "email_registration.remail.supply", "private_first", "private_first", "public_only"),
                    Text("remail_email_suffix", "邮箱后缀", "email_registration.remail.email_suffix", "outlook.com")),
                Section("Smailr",
                    Boolean("smailr_enabled", "启用", "email_registration.smailr.enabled", false),
                    Text("smailr_base_url", "API地址", "email_registration.smailr.base_url", "https://smailr.com"),
                    Secret("smailr_api_key", "API Key", "email_registration.smailr.api_key"),
                    Options("smailr_default_domain", "邮箱域名（LV1+）", "email_registration.smailr.default_domain", "smailr.com",
                        "smailr.com", "loc.cc", "mail.nodeloc.cc", "nodeloc.cc"),
                    Integer("smailr_timeout", "请求超时秒数", "email_registration.smailr.timeout", "30")),
                Section("CFWorker",
                    Text("cfworker_url", "Worker URL", "email_registration.cfworker_url"),
                    Text("cfworker_domain", "邮箱域名", "email_registration.cfworker_domain"),
                    Secret("cfworker_admin_token", "Admin Token", "email_registration.cfworker_admin_token"),
                    Secret("cfworker_api_token", "Cloudflare API Token", "email_registration.cfworker_api_token"))),

            Category("注册与接码",
                Section("SMSBower",
                    Secret("smsbower_api_key", "SMSBower API Key", "phone_reuse.smsbower.api_key"),
                    Integer("smsbower_sms_timeout", "短信等待秒", "phone_reuse.smsbower.sms_timeout"),
                    Integer("smsbower_sms_poll_interval", "短信轮询间隔秒", "phone_reuse.smsbower.sms_poll_interval"),
                    Integer("phone_max_reuse_count", "复用次数", "phone_reuse.max_reuse_count"),
                    Integer("phone_send_cooldown_seconds", "发码冷却秒", "phone_reuse.send_cooldown_seconds"),
                    Integer("phone_send_retry_attempts", "发码重试次数", "phone_reuse.send_retry_attempts"),
                    Integer("phone_send_retry_delay_seconds", "发码重试延迟秒", "phone_reuse.send_retry_delay_seconds"),
                    Text("phone_state_file", "状态文件", "phone_reuse.state_file")),
                Section("Codex OAuth",
                    Integer("codex_registration_timeout", "OAuth超时秒", "codex_oauth.registration_timeout"),
                    Boolean("codex_allow_passwordless_takeover", "允许邮箱OTP兜底", "codex_oauth.allow_passwordless_takeover", false),
                    Boolean("codex_auto_phone_verification", "自动手机验证", "codex_oauth.auto_phone_verification", false),
                    Boolean("codex_require_registration_refresh_token", "注册要求RT", "codex_oauth.require_registration_refresh_token", true),
                    Boolean("codex_require_registration_phone_verification", "注册要求手机号", "codex_oauth.require_registration_phone_verification", true)),
                Section("AT 稳定入库",
                    Integer("registration_at_stability_probe_count", "AT探测次数", "registration.at_stability_probe_count", "2"),
                    Integer("registration_at_stability_probe_delay", "探测间隔秒", "registration.at_stability_probe_delay_seconds", "10"),
                    Integer("registration_at_probe_timeout", "单次探测超时秒", "registration.at_probe_timeout_seconds", "30")),
                Section("阶段并发",
                    Integer("registration_network_concurrency", "注册网络并发", "registration.stage_concurrency.network", "4"),
                    Integer("registration_at_probe_concurrency", "AT探测并发", "registration.stage_concurrency.at_probe", "4")),
                Section("Sentinel",
                    Text("sentinel_version", "Sentinel版本", "email_registration.sentinel_version"),
                    Integer("sentinel_max_concurrency", "提取并发", "email_registration.sentinel_max_concurrency", "2"),
                    Integer("sentinel_prewarm_window", "一对一预热窗口", "email_registration.sentinel_prewarm_window", "4"),
                    Integer("sentinel_circuit_failures", "熔断失败次数", "email_registration.sentinel_circuit_failures", "3"),
                    Integer("sentinel_circuit_cooldown", "熔断冷却秒", "email_registration.sentinel_circuit_cooldown_seconds", "60"))),

            Category("导入与账号",
                Section("CPA",
                    Text("cpa_api_url", "CPA地址", "cpa_mode.api_url"),
                    Secret("cpa_api_token", "CPA Token", "cpa_mode.api_token")),
                Section("SUB2API",
                    Text("sub2api_url", "API地址", "sub2api.api_url"),
                    Secret("sub2api_token", "API Token", "sub2api.api_token"),
                    Text("sub2api_email", "登录邮箱", "sub2api.email"),
                    Secret("sub2api_password", "登录密码", "sub2api.password"),
                    Text("sub2api_group", "目标分组", "sub2api.group_name"),
                    Text("sub2api_group_ids", "分组ID", "sub2api.group_ids"),
                    Text("sub2api_proxy", "远端代理", "sub2api.proxy_name"),
                    Text("sub2api_proxy_id", "代理ID", "sub2api.proxy_id"),
                    Integer("sub2api_priority", "优先级", "sub2api.priority"),
                    Integer("sub2api_concurrency", "账号并发", "sub2api.concurrency"),
                    Options("sub2api_auth_mode", "凭据模式", "sub2api.auth_mode", "auto", "auto", "oauth", "agent_identity"),
                    Boolean("sub2api_verify_after_import", "导入后连通测试", "sub2api.verify_after_import", true))),

            Category("网络与支付",
                Section("基础网络",
                    Text("registration_proxy", "注册代理（主）", "", "http://127.0.0.1:7897"),
                    Multiline("registration_proxy_pool", "注册代理池", ""),
                    Text("mailbox_proxy", "邮箱收件代理", "", "http://127.0.0.1:7897")),
                Section("协议管理",
                    Text("protocol_enabled_methods", "启用方式", "", "paypal,gopay,gcash,grabpay,upi,ideal,pix,kakao,blik,twint,direct_card,momo"),
                    Text("protocol_reference_root", "提链器目录", "protocol_payments.reference_root", "services/protocol-payment"),
                    Text("protocol_state_file", "状态文件", "protocol_payments.state_file", "runtime/payment_link_runs.jsonl"),
                    Integer("protocol_timeout_seconds", "协议超时秒", "protocol_payments.timeout_seconds", "900")),
                Section("正式批量支付",
                    Integer("protocol_batch_momo_workers", "MoMo并发", "protocol_payments.batch.method_workers.momo", "2"),
                    Integer("protocol_batch_kakao_workers", "Kakao并发", "protocol_payments.batch.method_workers.kakao", "2"),
                    Integer("protocol_batch_gopay_workers", "GoPay并发", "protocol_payments.batch.method_workers.gopay", "2"),
                    Integer("protocol_batch_gcash_workers", "GCash并发", "protocol_payments.batch.method_workers.gcash", "2"),
                    Integer("protocol_batch_grabpay_workers", "GrabPay并发", "protocol_payments.batch.method_workers.grabpay", "2"),
                    Boolean("protocol_batch_pause_on_canary_failure", "Canary失败暂停", "protocol_payments.batch.pause_on_canary_failure", true),
                    Integer("protocol_batch_canary_pause_seconds", "暂停秒数", "protocol_payments.batch.canary_pause_seconds", "21600"),
                    Multiline("protocol_payment_matrix", "地区资格矩阵 JSON", "")),
                Section("PayPal",
                    Multiline("paypal_proxy", "PayPal代理池", ""),
                    Options("paypal_billing_region", "订单生成地区", "", "DE", "JP", "US", "AU", "DE", "FR", "GB", "IN", "BR"),
                    Options("paypal_link_generation_type", "PayPal直链生成模式", "paypal.link_generation_type", "hosted_long_url", "hosted_long_url", "paypal_direct", "paypal_direct_zero_due"))),

            Category("数据与文件",
                Section("运行环境",
                    Text("python_path", "Python解释器路径", "runtime.python_path", "python")),
                Section("本地存储",
                    Text("output_directory", "Session目录", "output.directory"),
                    Text("sqlite_path", "SQLite路径", "storage.sqlite_path")))
        };

        public static IEnumerable<SettingDefinition> AllFields => Categories.SelectMany(category => category.Sections).SelectMany(section => section.Fields);
    }
}
