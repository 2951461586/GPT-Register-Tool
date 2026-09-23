// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

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

        /// <summary>
        /// Re-resolve the provider-scoped fields in place for an explicit
        /// 供应商 selection. Called when the dropdown changes, so the API Key
        /// box cannot keep displaying another provider's key. See
        /// <see cref="SettingsService.ReloadProviderScopedFields"/>.
        /// </summary>
        void ReloadProviderScopedFields(IEnumerable<SettingFieldViewModel> fields, string provider);
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

            // JsonNode.Parse returns null for the literal `null`, which the
            // `is not JsonObject` check below rejects with the real message.
            JsonNode? matrix;
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
                // Provider-scoped settings are declared as
                // `phone_reuse.{provider}.<key>`. Substitute the selected
                // provider once, before the generic write loop, so the API-key
                // box cannot keep writing to `phone_reuse.smsbower.*` after the
                // operator switched to another provider -- a silent
                // wrong-provider write, where the key looks saved but the
                // backend reads a different section.
                string selectedProvider = SelectedProvider(FindValue(fields, "phone_provider"));
                foreach (SettingFieldViewModel field in fields.Where(field => field.Definition.JsonPath.Length > 0))
                {
                    if (field.Key is "python_path" or "token_file")
                        field.Value = NormalizePathSetting(field.Value, field.Definition.DefaultValue);
                    SetPath(root, ResolveProviderPath(field.Definition.JsonPath, selectedProvider), ToJsonValue(field));
                }

                // Registration must never silently fall back to a direct
                // connection when the settings box is left blank.
                string registrationProxy = ProxyInputNormalizer.Normalize(
                    First(Find(fields, "registration_proxy").Value.Trim(), LocalProxy));
                string mailboxProxy = ProxyInputNormalizer.Normalize(
                    First(Find(fields, "mailbox_proxy").Value.Trim(), LocalProxy));
                string[] mailboxPool = ProxyInputNormalizer.NormalizeList(
                        Find(fields, "mailbox_proxy_pool").Value)
                    .Where(value => !string.Equals(value, mailboxProxy, StringComparison.OrdinalIgnoreCase))
                    .ToArray();
                var orderedMailboxPool = new List<string> { mailboxProxy };
                orderedMailboxPool.AddRange(mailboxPool);
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
                SetPath(root, "mailbox_proxy_pool", ToArray(orderedMailboxPool));
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
                // phone_reuse.source is written by the catalog's provider
                // selector. Do NOT pin it here: the selector exists precisely so
                // the operator can choose a provider, and a late pin would
                // overwrite that choice with the default on every save.
                //
                // These two are migrations for configs written by older builds.
                // The static phone pool was removed on 2026-09-22 (see
                // docs/current/configuration.md); the keys have no reader left,
                // so leaving them would advertise settings that do nothing.
                RemovePath(root, "phone_reuse.phone_pool");
                RemovePath(root, "phone_reuse.smsbower.pool_size");
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
                JsonObject? root = ReadRootIfExists();
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
                JsonObject? root = ReadRootIfExists();
                if (root == null) return Array.Empty<string>();
                JsonNode? value = GetPath(root, path);
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

        private string ReadValue(JsonObject root, SettingDefinition definition)
            => ReadValue(root, definition, SelectedProvider(Text(root, "phone_reuse.source")));

        /// <summary>
        /// Value of <paramref name="definition"/> resolved as if
        /// <paramref name="provider"/> were the 供应商 selection.
        ///
        /// <para>
        /// The provider is a parameter instead of being read from
        /// `phone_reuse.source` inside, because the dialog can change that
        /// selection before anything is saved -- see
        /// <see cref="ReloadProviderScopedFields"/>.
        /// </para>
        /// </summary>
        private string ReadValue(JsonObject root, SettingDefinition definition, string provider)
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
                "mailbox_proxy_pool" => First(
                    ListText(root, "mailbox_proxy_pool"),
                    Text(root, "mailbox_proxy"),
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
                "token_file" or "python_path" => NormalizePathSetting(
                    Text(root, definition.JsonPath), definition.DefaultValue),
                // Provider-scoped fields resolve against the provider passed in,
                // so switching provider in the dialog shows that provider's own
                // key rather than the previously selected one's -- provided the
                // dialog actually re-resolves them, which is what
                // ReloadProviderScopedFields is for. Resolving here once, at
                // Load time, is not enough: the operator changes the selector
                // afterwards.
                _ when IsProviderScoped(definition) => Text(
                    root,
                    ResolveProviderPath(definition.JsonPath, provider)),
                _ => Text(root, definition.JsonPath)
            };
            return string.IsNullOrWhiteSpace(value) ? definition.DefaultValue : value;
        }

        /// <summary>
        /// Whether a catalog entry lives under the selected SMS provider, i.e.
        /// its path carries the <c>{provider}</c> placeholder.
        /// </summary>
        internal static bool IsProviderScoped(SettingDefinition definition)
            => definition.JsonPath.Contains(ProviderToken, StringComparison.Ordinal);

        /// <summary>
        /// Re-resolve every provider-scoped field against an explicit provider
        /// selection, in place, without writing anything to disk.
        ///
        /// <para>
        /// 🔴 This exists because the 供应商 dropdown and the API Key box are
        /// **separate controls that must stay in step**. Resolving
        /// provider-scoped paths only at <see cref="Load"/> time leaves the box
        /// showing the provider that was selected when the dialog opened. The
        /// operator then picks a different provider and saves, and
        /// <see cref="Save"/> -- correctly, by its own contract -- writes the
        /// displayed value to the **newly selected** section. The result is not
        /// a harmless no-op but a cross-provider overwrite: measured on
        /// 2026-09-23, one pass through the dropdown stamped smsbower's key into
        /// `phone_reuse.herosms`, `.grizzly` and `.nexsms`, plus its empty
        /// endpoint and its `sms_timeout`. All three vendors then answered with a
        /// credential error (herosms `401 BAD_KEY`, grizzly `NO_KEY`, nexsms
        /// `401`), which the desktop surfaced as "无法读取 OpenAI 号码地区和价格档位"
        /// -- a message that names neither the key nor the section, so the
        /// config looked broken rather than stale.
        /// </para>
        ///
        /// <para>
        /// Note the earlier `ResolveProviderPath` fix in <see cref="Save"/>
        /// addressed the write *target*; this addresses the displayed *value*.
        /// Both are needed, and fixing only the first is what turned a
        /// misdirected write into a silently mislabelled one.
        /// </para>
        /// </summary>
        public void ReloadProviderScopedFields(IEnumerable<SettingFieldViewModel> fields, string provider)
        {
            JsonObject? root = ReadRootIfExists();
            if (root == null) return;
            string resolved = SelectedProvider(provider);
            foreach (SettingFieldViewModel field in fields.Where(field => IsProviderScoped(field.Definition)))
            {
                field.Value = ReadValue(root, field.Definition, resolved);
            }
        }

        /// <summary>
        /// Keep paths inside the repository portable by storing them relative to
        /// the application root. External absolute paths remain absolute because
        /// a relative value cannot represent a location outside this checkout.
        /// </summary>
        private string NormalizePathSetting(string raw, string fallback)
        {
            string value = (raw ?? "").Trim();
            if (value.Length == 0) return fallback;

            string expanded;
            try
            {
                expanded = Environment.ExpandEnvironmentVariables(value);
                if (Path.IsPathRooted(expanded))
                {
                    string root = Path.GetFullPath(_paths.RootDirectory)
                        .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                    string candidate = Path.GetFullPath(expanded);
                    string rootPrefix = root + Path.DirectorySeparatorChar;
                    if (string.Equals(candidate, root, StringComparison.OrdinalIgnoreCase))
                        return ".";
                    if (candidate.StartsWith(rootPrefix, StringComparison.OrdinalIgnoreCase))
                        return NormalizePathSeparators(Path.GetRelativePath(root, candidate));
                    return value;
                }
            }
            catch (Exception)
            {
                // Preserve an invalid operator-entered path for the existing
                // runtime error message rather than failing the whole settings save.
                return value;
            }

            return NormalizePathSeparators(value);
        }

        private static string NormalizePathSeparators(string value)
            => value.Replace('\\', '/');

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

        /// <summary>
        /// Placeholder the catalog uses for settings that live under the
        /// selected SMS provider (`phone_reuse.&lt;provider&gt;.*`). Substituted at
        /// read and save time -- see <see cref="ResolveProviderPath"/>.
        /// </summary>
        private const string ProviderToken = "{provider}";

        /// <summary>
        /// Non-throwing sibling of <see cref="Find"/>, for lookups where an
        /// absent field must not abort the save.
        /// </summary>
        private static string FindValue(IEnumerable<SettingFieldViewModel> fields, string key)
            => fields.FirstOrDefault(field => string.Equals(field.Key, key, StringComparison.Ordinal))?.Value ?? "";

        /// <summary>
        /// Path segment for provider-scoped settings. Falls back to the default
        /// provider when the selector is blank, because an empty value must
        /// still produce a valid path.
        ///
        /// A hand-edited alias (for example "hero_sms") is deliberately NOT
        /// canonicalised here: that would need a second copy of the Python alias
        /// map in C#, which is exactly the hand-mirrored registry that drifts.
        /// The failure stays loud instead -- Python resolves the alias, finds no
        /// `phone_reuse.herosms.api_key`, and reports `phone_pool_unavailable`
        /// naming the key to set.
        /// </summary>
        private static string SelectedProvider(string raw)
        {
            string value = (raw ?? "").Trim().ToLowerInvariant();
            return value.Length > 0 ? value : SettingsCatalog.DefaultPhoneProvider;
        }

        private static string ResolveProviderPath(string path, string provider)
            => path.Contains(ProviderToken, StringComparison.Ordinal)
                ? path.Replace(ProviderToken, provider, StringComparison.Ordinal)
                : path;

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
            JsonNode? value = GetPath(root, path);
            if (value is JsonArray array)
                return string.Join(",", array.Select(item => item?.ToString() ?? "").Where(item => item.Length > 0));
            return value?.ToString() ?? "";
        }

        private static string ListText(JsonObject root, string path)
        {
            JsonNode? value = GetPath(root, path);
            IEnumerable<string> entries = value is JsonArray array
                ? array.Select(item => item?.ToString() ?? "")
                : ParseList(value?.ToString() ?? "");
            return string.Join(ProxyInputNormalizer.LineSeparator, entries.Where(item => item.Length > 0));
        }

        private static string FirstArray(JsonObject root, string path)
            => GetPath(root, path) is JsonArray array && array.Count > 0 ? array[0]?.ToString() ?? "" : "";

        private static string Text(JsonObject root, string path) => GetPath(root, path)?.ToString() ?? "";

        // Elements may be null: Environment.GetEnvironmentVariable returns null
        // for an unset variable, and "the env var is not set" has to be
        // distinguishable from "it is set to empty".
        private static string First(params string?[] values)
            => values.FirstOrDefault(value => !string.IsNullOrWhiteSpace(value)) ?? "";

        /// Returns null when any segment of the dotted path is missing or is not
        /// an object - callers use it to decide between "unset" and "set to
        /// something", so a fabricated empty node would be wrong here.
        private static JsonNode? GetPath(JsonObject root, string? path)
        {
            JsonNode? current = root;
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
