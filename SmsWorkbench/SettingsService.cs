using System.Text.Json;
using System.Text.Json.Nodes;

namespace SmsWorkbench
{
    public interface ISettingsService
    {
        string ConfigPath { get; }
        IReadOnlyList<SettingsCategoryViewModel> Load();
        SettingsSaveResult Save(IEnumerable<SettingsCategoryViewModel> categories);
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
            ConfigPath = Path.Combine(paths.RootDirectory, "config.json");
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

                string registrationProxy = Find(fields, "registration_proxy").Value.Trim();
                string mailboxProxy = First(Find(fields, "mailbox_proxy").Value.Trim(), LocalProxy);
                string[] registrationPool = ParseList(Find(fields, "registration_proxy_pool").Value)
                    .Where(value => !string.Equals(value, registrationProxy, StringComparison.OrdinalIgnoreCase))
                    .ToArray();
                var orderedRegistrationPool = new List<string>();
                if (registrationProxy.Length > 0) orderedRegistrationPool.Add(registrationProxy);
                orderedRegistrationPool.AddRange(registrationPool);
                SetPath(root, "proxy.registration", registrationProxy);
                SetPath(root, "proxy.default", registrationProxy);
                SetPath(root, "proxy.pool", ToArray(orderedRegistrationPool));
                SetPath(root, "mailbox_proxy", mailboxProxy);
                SetPath(root, "phone_reuse.proxy", registrationProxy);

                SetPath(root, "protocol_payments.proxy_pool", ToArray(ParseList(Find(fields, "protocol_proxy_pool").Value)));
                SetPath(root, "protocol_payments.enabled_methods", ToArray(ParseList(Find(fields, "protocol_enabled_methods").Value)));
                SetPath(root, "protocol_payments.matrix", matrix);
                SetPath(root, "paypal.proxies", ToArray(new[] { Find(fields, "paypal_proxy").Value.Trim() }));
                SetPath(root, "paypal.billing_regions", ToArray(new[] { Find(fields, "paypal_billing_region").Value.Trim().ToUpperInvariant() }));

                SetPath(root, "email_registration.remail.service_mode", "purchase");
                SetPath(root, "phone_reuse.source", "smsbower");
                SetPath(root, "phone_reuse.smsbower.service", "dr");
                SetPath(root, "phone_reuse.smsbower.service_name", "OpenAI (ChatGPT)");
                RemovePath(root, "phone_reuse.smsbower.pool_size");
                RemovePath(root, "phone_reuse.phone_pool");
                RemovePath(root, "protocol_payments.methods.blik.blik_code");

                WriteAtomic(root);
                return new SettingsSaveResult(true);
            }
            catch (Exception exception)
            {
                return new SettingsSaveResult(false, "配置保存失败：" + exception.Message);
            }
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
                "protocol_proxy_pool" => ListText(root, "protocol_payments.proxy_pool"),
                "protocol_enabled_methods" => ArrayText(root, "protocol_payments.enabled_methods"),
                "protocol_payment_matrix" => GetPath(root, "protocol_payments.matrix")?.ToJsonString(IndentedJson)
                    ?? "{\n  \"cells\": []\n}",
                "paypal_proxy" => FirstArray(root, "paypal.proxies"),
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
            EnsureConfigFile();
            return JsonNode.Parse(File.ReadAllText(ConfigPath, Encoding.UTF8)) as JsonObject ?? new JsonObject();
        }

        private void EnsureConfigFile()
        {
            if (File.Exists(ConfigPath)) return;
            string example = Path.Combine(_paths.RootDirectory, "config.example.json");
            if (File.Exists(example)) File.Copy(example, ConfigPath);
            else File.WriteAllText(ConfigPath, "{}", new UTF8Encoding(false));
        }

        private void WriteAtomic(JsonObject root)
        {
            string temporary = ConfigPath + ".tmp." + Guid.NewGuid().ToString("N");
            try
            {
                File.WriteAllText(
                    temporary,
                    root.ToJsonString(IndentedJson),
                    new UTF8Encoding(false));
                File.Move(temporary, ConfigPath, overwrite: true);
            }
            finally
            {
                try
                {
                    if (File.Exists(temporary)) File.Delete(temporary);
                }
                catch
                {
                }
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
            return string.Join(Environment.NewLine, entries.Where(item => item.Length > 0));
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
    }
}
