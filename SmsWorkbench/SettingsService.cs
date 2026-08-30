using System.Text.Json;
using System.Text.Json.Nodes;

namespace SmsWorkbench
{
    public interface ISettingsService
    {
        string ConfigPath { get; }
        IReadOnlyList<SettingsCategoryViewModel> Load();
        SettingsSaveResult Save(IEnumerable<SettingsCategoryViewModel> categories);
        string GetString(string path, string fallback = "");
        IReadOnlyList<string> GetStringList(string path);
        void UpdateConfig(Action<JsonObject> mutate);
    }

    public sealed class SettingsService : ISettingsService
    {
        private const string LocalProxy = "http://127.0.0.1:7897";
        private static readonly string[] ListSeparators = { "\r\n", "\n", "," };
        private static readonly JsonSerializerOptions IndentedJson = new() { WriteIndented = true };
        private readonly IApplicationPaths _paths;

        public SettingsService(IApplicationPaths paths)
        {
            _paths = paths;
            // Configuration now lives in the proxy/runtime/payment shard files
            // under the application root; expose the directory so "Open config"
            // reveals the full sharded layout.
            ConfigPath = paths.RootDirectory;
        }

        public string ConfigPath { get; }

        public IReadOnlyList<SettingsCategoryViewModel> Load()
        {
            JsonObject root = ReadRoot();
            return SettingsCatalog.Categories.Select(category => new SettingsCategoryViewModel(
                category.Title,
                category.Sections.Select(section => new SettingsSectionViewModel(
                    section.Title,
                    section.Fields.Select(definition => new SettingFieldViewModel(
                        definition,
                        ReadValue(root, definition))))))).ToArray();
        }

        public SettingsSaveResult Save(IEnumerable<SettingsCategoryViewModel> categories)
        {
            SettingFieldViewModel[] fields = categories
                .SelectMany(category => category.Sections)
                .SelectMany(section => section.Fields)
                .ToArray();
            foreach (SettingFieldViewModel field in fields.Where(field => field.Kind == SettingFieldKind.Number))
            {
                if (field.Value.Trim().Length > 0 && !int.TryParse(field.Value.Trim(), out _))
                    return new SettingsSaveResult(false, field.Label + " 必须是整数。");
            }

            JsonNode matrix;
            try
            {
                matrix = JsonNode.Parse(Find(fields, "protocol_payment_matrix").Value);
                if (matrix is not JsonObject)
                    return new SettingsSaveResult(false, "地区资格矩阵根节点必须是 JSON 对象。");
            }
            catch (Exception exception)
            {
                return new SettingsSaveResult(false, "地区资格矩阵 JSON 无效：" + exception.Message);
            }

            try
            {
                JsonObject root = ReadRoot();
                foreach (SettingFieldViewModel field in fields.Where(field => field.Definition.JsonPath.Length > 0))
                    SetPath(root, field.Definition.JsonPath, ToJsonValue(field));

                // Registration must never silently fall back to a direct
                // connection when the settings box is left blank.
                string registrationProxy = ProxyInputNormalizer.Normalize(
                    First(Find(fields, "registration_proxy").Value.Trim(), LocalProxy));
                string mailboxProxy = ProxyInputNormalizer.Normalize(
                    First(Find(fields, "mailbox_proxy").Value.Trim(), LocalProxy));
                string[] registrationPool = ProxyInputNormalizer.NormalizeList(
                        Find(fields, "registration_proxy_pool").Value)
                    .Where(value => !string.Equals(value, registrationProxy, StringComparison.OrdinalIgnoreCase))
                    .ToArray();
                var orderedRegistrationPool = new List<string>();
                orderedRegistrationPool.Add(registrationProxy);
                orderedRegistrationPool.AddRange(registrationPool);
                SetPath(root, "proxy.registration", registrationProxy);
                SetPath(root, "proxy.default", registrationProxy);
                SetPath(root, "proxy.pool", ToArray(orderedRegistrationPool));
                SetPath(root, "mailbox_proxy", mailboxProxy);
                SetPath(root, "phone_reuse.proxy", registrationProxy);

                // The shared protocol proxy pool is intentionally no longer
                // editable from Settings.  Batch protocol payment owns its
                // checkout/approve pools; preserve any legacy global value.
                SetPath(root, "protocol_payments.enabled_methods", ToArray(ParseList(Find(fields, "protocol_enabled_methods").Value)));
                SetPath(root, "protocol_payments.matrix", matrix);
                SetPath(root, "paypal.proxies", ToArray(ProxyInputNormalizer.NormalizeList(
                    Find(fields, "paypal_proxy").Value)));
                SetPath(root, "paypal.billing_regions", ToArray(new[] { Find(fields, "paypal_billing_region").Value.Trim().ToUpperInvariant() }));

                // Python's mailbox_remail falls back to service_mode "code" when the key is
                // absent, but every desktop-driven ReMail acquisition runs in "purchase"
                // mode; keep pinning it so saving unrelated settings cannot silently
                // switch the purchase flow back to code mode.
                SetPath(root, "email_registration.remail.service_mode", "purchase");
                // phone_reuse.py defaults source to "auto", which falls back to the static
                // phone pool when no SMSBower key is configured.  The desktop surface
                // intentionally dropped static phone-pool editing, so keep pinning the
                // SMSBower seam here.
                SetPath(root, "phone_reuse.source", "smsbower");
                RemovePath(root, "phone_reuse.smsbower.pool_size");
                RemovePath(root, "phone_reuse.phone_pool");
                RemovePath(root, "protocol_payments.methods.blik.blik_code");
                RemovePath(root, "agent_identity.register_on_free_signup");
                RemovePath(root, "agent_identity.registration_timeout");
                RemoveEmptyObject(root, "agent_identity");

                WriteAtomic(root);
                return new SettingsSaveResult(true);
            }
            catch (Exception exception)
            {
                return new SettingsSaveResult(false, "配置保存失败：" + exception.Message);
            }
        }

        public string GetString(string path, string fallback = "")
        {
            try
            {
                JsonObject root = ReadRootIfExists();
                string value = root == null ? "" : Text(root, path);
                return string.IsNullOrWhiteSpace(value) ? fallback : value;
            }
            catch
            {
                return fallback;
            }
        }

        public IReadOnlyList<string> GetStringList(string path)
        {
            try
            {
                JsonObject root = ReadRootIfExists();
                if (root == null) return Array.Empty<string>();
                JsonNode value = GetPath(root, path);
                if (value is JsonArray array)
                    return array.Select(item => item?.ToString() ?? "").Where(item => item.Length > 0).ToArray();
                string single = value?.ToString() ?? "";
                return single.Length > 0 ? new[] { single } : Array.Empty<string>();
            }
            catch
            {
                return Array.Empty<string>();
            }
        }

        public void UpdateConfig(Action<JsonObject> mutate)
        {
            JsonObject root = ReadRoot();
            mutate(root);
            WriteAtomic(root);
        }

        // Read-only access used by MainWindow helpers.  Unlike Load/Save this never
        // creates config and parses case-insensitively, matching the legacy
        // dictionary-based readers it replaces; any failure yields the fallback.
        // The merged root (from the proxy/runtime/payment shards, migrated from a
        // legacy config.json on first load) is cached on a signature of the
        // underlying files' existence/mtime/size so hot loops reading settings
        // (account-grid refresh reads the file once per row) no longer re-read and
        // re-parse on every GetString.
        private JsonObject? cachedRoot;
        private string cachedSignature = "";
        private readonly object rootCacheLock = new();

        private JsonObject? ReadRootIfExists()
        {
            lock (rootCacheLock)
            {
                string signature = ConfigSignature();
                if (cachedRoot is not null && signature == cachedSignature)
                    return cachedRoot;
                cachedRoot = ConfigStore.ReadMerged(_paths);
                cachedSignature = signature;
                return cachedRoot;
            }
        }

        private string ConfigSignature()
        {
            var builder = new System.Text.StringBuilder();
            foreach (string file in ConfigStore.AllConfigFiles(_paths))
            {
                if (File.Exists(file))
                {
                    FileInfo info = new(file);
                    builder.Append('E').Append(info.LastWriteTimeUtc.Ticks)
                        .Append(':').Append(info.Length).Append('|');
                }
                else
                {
                    builder.Append('M').Append('|');
                }
            }
            return builder.ToString();
        }

        private static string ReadValue(JsonObject root, SettingDefinition definition)
        {
            string value = definition.Key switch
            {
                "registration_proxy" => First(
                    Text(root, "proxy.registration"),
                    Text(root, "registration_proxy"),
                    FirstArray(root, "paypal.proxies"),
                    Text(root, "proxy.default"),
                    LocalProxy),
                "registration_proxy_pool" => First(ListText(root, "proxy.pool"), Text(root, "proxy.registration")),
                "mailbox_proxy" => First(
                    Text(root, "mailbox_proxy"),
                    Text(root, "email_registration.mailbox_proxy"),
                    Text(root, "proxy.mailbox"),
                    LocalProxy),
                "smailr_api_key" => First(
                    Text(root, definition.JsonPath),
                    Environment.GetEnvironmentVariable("SMAILR_API_KEY")),
                "protocol_proxy_pool" => ListText(root, "protocol_payments.proxy_pool"),
                "protocol_enabled_methods" => ArrayText(root, "protocol_payments.enabled_methods"),
                "protocol_payment_matrix" => GetPath(root, "protocol_payments.matrix")?.ToJsonString(IndentedJson)
                    ?? "{\n  \"cells\": []\n}",
                "paypal_proxy" => ListText(root, "paypal.proxies"),
                "paypal_billing_region" => First(
                    FirstArray(root, "paypal.billing_regions"),
                    Text(root, "paypal.billing_region"),
                    Text(root, "paypal.billing_country"),
                    "DE").ToUpperInvariant(),
                _ => Text(root, definition.JsonPath)
            };
            return string.IsNullOrWhiteSpace(value) ? definition.DefaultValue : value;
        }

        private JsonObject ReadRoot()
        {
            // Merge the proxy/runtime/payment shards (or migrate a legacy single
            // config.json); an empty object keeps the save path functional before
            // any configuration exists.
            return ConfigStore.ReadMerged(_paths) ?? new JsonObject();
        }

        private void WriteAtomic(JsonObject root)
        {
            // Persist the merged configuration back into the proxy/runtime/payment
            // shard files, routing each top-level key to its owning shard. This is
            // the single write boundary shared by Settings and the batch payment
            // service.
            ConfigStore.WriteShards(_paths, root);
            lock (rootCacheLock)
            {
                cachedRoot = null; // force re-merge on next read
                cachedSignature = "";
            }
        }

        private static JsonValue ToJsonValue(SettingFieldViewModel field)
        {
            string value = field.Value.Trim();
            return field.Kind switch
            {
                SettingFieldKind.Number when int.TryParse(value, out int number) => JsonValue.Create(number),
                SettingFieldKind.Boolean => JsonValue.Create(field.BooleanValue),
                _ => JsonValue.Create(value)
            };
        }

        private static SettingFieldViewModel Find(IEnumerable<SettingFieldViewModel> fields, string key)
            => fields.First(field => string.Equals(field.Key, key, StringComparison.Ordinal));

        private static string[] ParseList(string value)
            => (value ?? "")
                .Split(ListSeparators, StringSplitOptions.RemoveEmptyEntries)
                .Select(item => item.Trim())
                .Where(item => item.Length > 0)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToArray();

        private static JsonArray ToArray(IEnumerable<string> values)
            => new(values.Select(value => (JsonNode)JsonValue.Create(value)).ToArray());

        private static string ArrayText(JsonObject root, string path)
        {
            JsonNode value = GetPath(root, path);
            if (value is JsonArray array)
                return string.Join(",", array.Select(item => item?.ToString() ?? "").Where(item => item.Length > 0));
            return value?.ToString() ?? "";
        }

        private static string ListText(JsonObject root, string path)
        {
            JsonNode value = GetPath(root, path);
            IEnumerable<string> entries = value is JsonArray array
                ? array.Select(item => item?.ToString() ?? "")
                : ParseList(value?.ToString() ?? "");
            return string.Join(ProxyInputNormalizer.LineSeparator, entries.Where(item => item.Length > 0));
        }

        private static string FirstArray(JsonObject root, string path)
            => GetPath(root, path) is JsonArray array && array.Count > 0 ? array[0]?.ToString() ?? "" : "";

        private static string Text(JsonObject root, string path) => GetPath(root, path)?.ToString() ?? "";

        private static string First(params string[] values)
            => values.FirstOrDefault(value => !string.IsNullOrWhiteSpace(value)) ?? "";

        private static JsonNode GetPath(JsonObject root, string path)
        {
            JsonNode current = root;
            foreach (string segment in (path ?? "").Split('.', StringSplitOptions.RemoveEmptyEntries))
            {
                if (current is not JsonObject map || !map.TryGetPropertyValue(segment, out current)) return null;
            }
            return current;
        }

        private static void SetPath(JsonObject root, string path, JsonNode value)
        {
            string[] segments = path.Split('.', StringSplitOptions.RemoveEmptyEntries);
            JsonObject current = root;
            for (int index = 0; index < segments.Length - 1; index++)
            {
                if (current[segments[index]] is not JsonObject child)
                {
                    child = new JsonObject();
                    current[segments[index]] = child;
                }
                current = child;
            }
            current[segments[^1]] = value;
        }

        private static void RemovePath(JsonObject root, string path)
        {
            string[] segments = path.Split('.', StringSplitOptions.RemoveEmptyEntries);
            JsonObject current = root;
            for (int index = 0; index < segments.Length - 1; index++)
            {
                if (current[segments[index]] is not JsonObject child) return;
                current = child;
            }
            current.Remove(segments[^1]);
        }

        private static void RemoveEmptyObject(JsonObject root, string propertyName)
        {
            if (root[propertyName] is JsonObject value && value.Count == 0)
                root.Remove(propertyName);
        }
    }
}
