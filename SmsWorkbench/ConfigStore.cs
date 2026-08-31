using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace SmsWorkbench
{
    /// <summary>
    /// Sharded configuration store. The historical single config.json is split
    /// into proxy.json / runtime.json / payment.json. Both the desktop shell and
    /// the Python backend merge these shards at load time, and a legacy single
    /// config.json is migrated into shards on first load. This mirrors
    /// sms_tool/config.py's load_merged_config / SHARD_OWNERSHIP exactly so the
    /// two languages keep a single, identical source of truth.
    /// </summary>
    public static class ConfigStore
    {
        public const string ProxyShard = "proxy.json";
        public const string RuntimeShard = "runtime.json";
        public const string PaymentShard = "payment.json";
        public const string LegacyConfig = "config.json";

        private static readonly string[] ShardFiles = { ProxyShard, RuntimeShard, PaymentShard };

        // Top-level config key -> owning shard file name. Mirrors
        // sms_tool/config.py SHARD_OWNERSHIP; unknown keys default to runtime.json
        // (matching Python's _split_into_shards fallback).
        private static readonly Dictionary<string, string> ShardOwnership = new()
        {
            // runtime.json
            ["runtime"] = RuntimeShard,
            ["timeouts"] = RuntimeShard,
            ["storage"] = RuntimeShard,
            ["output"] = RuntimeShard,
            ["account_health"] = RuntimeShard,
            ["registration"] = RuntimeShard,
            ["chatgpt"] = RuntimeShard,
            ["email_registration"] = RuntimeShard,
            ["codex_oauth"] = RuntimeShard,
            // proxy.json
            ["proxy"] = ProxyShard,
            ["mailbox_proxy"] = ProxyShard,
            ["phone_reuse"] = ProxyShard,
            ["paypal_browser"] = ProxyShard,
            // payment.json
            ["paypal"] = PaymentShard,
            ["paypal_nocard"] = PaymentShard,
            ["upi"] = PaymentShard,
            ["omakse"] = PaymentShard,
            ["protocol_payments"] = PaymentShard,
            ["kakao"] = PaymentShard,
            ["momo"] = PaymentShard,
            ["cpa_mode"] = PaymentShard,
            ["sub2api"] = PaymentShard,
        };

        private static readonly JsonSerializerOptions IndentedJson = new() { WriteIndented = true };

        /// <summary>Every file that participates in configuration, in the order
        /// used by the cache signature (shards first, then the legacy file).</summary>
        public static IReadOnlyList<string> AllConfigFiles(IApplicationPaths paths)
        {
            string root = paths.RootDirectory;
            return new[]
            {
                Path.Combine(root, ProxyShard),
                Path.Combine(root, RuntimeShard),
                Path.Combine(root, PaymentShard),
                Path.Combine(root, LegacyConfig),
            };
        }

        public static bool AnyShardExists(IApplicationPaths paths)
        {
            string root = paths.RootDirectory;
            foreach (string file in ShardFiles)
            {
                if (File.Exists(Path.Combine(root, file)))
                    return true;
            }
            return false;
        }

        /// <summary>
        /// Merge the proxy/runtime/payment shards into a single config object.
        /// Honors a legacy single config.json by migrating it into shards on first
        /// load (the legacy file is left in place, mirroring the Python backend).
        /// Returns null when no configuration exists.
        /// </summary>
        public static JsonObject? ReadMerged(IApplicationPaths paths)
        {
            string root = paths.RootDirectory;
            JsonObject? merged = null;
            if (AnyShardExists(paths))
            {
                merged = new JsonObject();
                foreach (string file in ShardFiles)
                {
                    string path = Path.Combine(root, file);
                    if (!File.Exists(path)) continue;
                    if (TryParse(path) is JsonObject obj)
                        DeepMerge(merged, obj);
                }
            }
            else
            {
                string legacy = Path.Combine(root, LegacyConfig);
                if (File.Exists(legacy) && TryParse(legacy) is JsonObject legacyObj)
                {
                    // Migrate the legacy single file into shards so subsequent reads
                    // use the sharded layout. The legacy file stays on disk (parity
                    // with the Python backend), and shards take precedence thereafter.
                    WriteShards(paths, legacyObj);
                    merged = new JsonObject();
                    DeepMerge(merged, legacyObj);
                }
            }

            if (merged is null) return null;

            // Re-serialize and re-parse with PropertyNameCaseInsensitive so that
            // downstream TryGetPropertyValue calls (GetString/GetPath) are
            // case-insensitive, matching the legacy single-file behavior where
            // JsonNode.Parse was called with that option. JsonObject created via
            // new() does not inherit JsonNodeOptions, so we round-trip here.
            return JsonNode.Parse(
                merged.ToJsonString(),
                new JsonNodeOptions { PropertyNameCaseInsensitive = true }) as JsonObject;
        }

        private static JsonNode? TryParse(string path)
        {
            try
            {
                return JsonNode.Parse(
                    File.ReadAllText(path, Encoding.UTF8),
                    new JsonNodeOptions { PropertyNameCaseInsensitive = true });
            }
            catch
            {
                return null;
            }
        }

        /// <summary>
        /// Split a merged config object into the owned shard files. Each top-level
        /// key is routed to its owning shard by ShardOwnership. A shard that ends
        /// up with no keys has its file removed, otherwise the stale file would be
        /// merged back on the next ReadMerged and resurrect keys the caller just
        /// deleted.
        /// </summary>
        public static void WriteShards(IApplicationPaths paths, JsonObject root)
        {
            var buckets = new Dictionary<string, JsonObject>
            {
                [ProxyShard] = new JsonObject(),
                [RuntimeShard] = new JsonObject(),
                [PaymentShard] = new JsonObject(),
            };
            foreach (var pair in root)
            {
                if (pair.Value is null) continue;
                string owner = ShardOwnership.TryGetValue(pair.Key, out string? shard) ? shard : RuntimeShard;
                buckets[owner][pair.Key] = pair.Value.DeepClone();
            }
            foreach (var bucket in buckets)
            {
                if (bucket.Value.Count == 0)
                    DeleteShard(paths, bucket.Key);
                else
                    WriteAtomic(paths, bucket.Key, bucket.Value);
            }
        }

        private static void DeleteShard(IApplicationPaths paths, string fileName)
        {
            try
            {
                string path = Path.Combine(paths.RootDirectory, fileName);
                if (File.Exists(path)) File.Delete(path);
            }
            catch
            {
                // best-effort cleanup
            }
        }

        private static void DeepMerge(JsonObject target, JsonObject source)
        {
            foreach (var pair in source)
            {
                if (pair.Value is null) continue;
                if (target.TryGetPropertyValue(pair.Key, out JsonNode? existing)
                    && existing is JsonObject existingObj
                    && pair.Value is JsonObject incomingObj)
                {
                    DeepMerge(existingObj, incomingObj);
                }
                else
                {
                    target[pair.Key] = pair.Value.DeepClone();
                }
            }
        }

        private static void WriteAtomic(IApplicationPaths paths, string fileName, JsonObject content)
        {
            string path = Path.Combine(paths.RootDirectory, fileName);
            string temporary = path + ".tmp." + Guid.NewGuid().ToString("N");
            try
            {
                File.WriteAllText(temporary, content.ToJsonString(IndentedJson), new UTF8Encoding(false));
                File.Move(temporary, path, overwrite: true);
            }
            finally
            {
                try
                {
                    if (File.Exists(temporary)) File.Delete(temporary);
                }
                catch
                {
                    // best-effort cleanup
                }
            }
        }
    }
}
