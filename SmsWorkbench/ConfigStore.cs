// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System.Text;
using System.Text.Encodings.Web;
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
            ["mailbox_proxy_pool"] = ProxyShard,
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

        private static readonly JsonSerializerOptions IndentedJson = new()
        {
            WriteIndented = true,
            // Python serializes shards with json.dumps(..., ensure_ascii=False),
            // which emits "+" and CJK literally. The default JavaScriptEncoder
            // escapes both ("\u002B", "\u667A\u5229"), so a desktop save rewrote
            // every non-ASCII byte in the file -- and the change-detection in
            // WriteAtomic then saw a "changed" file on the Python side forever.
            // UnsafeRelaxedJsonEscaping is the matching setting; the "unsafe" in
            // its name refers to embedding JSON in HTML, which a config file
            // never does. Observed before the fix: proxy.json held
            // "\u667A\u5229" for what should have been "智利".
            Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        };

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

        /// <summary>
        /// True when the sharded layout is in use. An empty <c>{}</c> shard counts
        /// as present, which is the whole point of writing empty shards rather than
        /// deleting them: this predicate must not flip back to false -- and hand
        /// the app to the legacy branch -- just because the operator cleared every
        /// setting.
        /// </summary>
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
        /// key is routed to its owning shard by ShardOwnership.
        ///
        /// <para>
        /// A shard that ends up with no keys is written as <c>{}</c> instead of
        /// having its file deleted. Both languages read "at least one shard file
        /// exists" as "the sharded layout is in use", so deleting all three used to
        /// flip the app back onto the legacy single-file branch and resurrect
        /// configuration the caller had just removed. An empty object merges to
        /// nothing, so it still cannot bring a deleted key back -- the protection
        /// the delete existed for is preserved, without the layout-flipping.
        /// </para>
        ///
        /// <para>
        /// Returns the shard file names actually written. A shard whose serialized
        /// content already matches the file on disk is skipped: its mtime stays put
        /// (so "which shard changed" is again a usable audit signal) and the
        /// previous <c>.bak</c> is not overwritten by a save that changed nothing.
        /// Callers that need "all three files exist" must assert on the files, not
        /// on this list -- a fully unchanged save legitimately returns an empty one.
        /// </para>
        /// </summary>
        public static IReadOnlyList<string> WriteShards(IApplicationPaths paths, JsonObject root)
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
            var written = new List<string>();
            foreach (string file in ShardFiles)
            {
                if (WriteAtomic(paths, file, buckets[file]))
                    written.Add(file);
            }
            return written;
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

        /// <summary>
        /// Atomic, change-detecting write of a single shard.
        ///
        /// <para>
        /// Mirrors Python's <c>_atomic_write_json</c> step for step: a temp file
        /// beside the target, a <c>.bak</c> of the previous content, an fsync
        /// before the rename, and a no-op when the payload already matches the
        /// file. The desktop previously did none of that, so "the desktop writes
        /// and Python keeps the backup" was a chain that never actually ran, and a
        /// crash mid-write could leave a truncated shard -- which <em>is</em> the
        /// entire application configuration.
        /// </para>
        ///
        /// <returns><c>true</c> when the file was written.</returns>
        /// </summary>
        private static bool WriteAtomic(IApplicationPaths paths, string fileName, JsonObject content)
        {
            string path = Path.Combine(paths.RootDirectory, fileName);
            // Trailing newline to match Python's `json.dumps(...) + "\n"`. The two
            // writers must agree byte-for-byte, otherwise each side's change
            // detection sees the other side's file as modified.
            string payload = content.ToJsonString(IndentedJson) + "\n";
            if (ReadTextOrNull(path) == payload) return false;

            string temporary = path + ".tmp." + Guid.NewGuid().ToString("N");
            try
            {
                if (File.Exists(path))
                {
                    try
                    {
                        File.Copy(path, path + ".bak", overwrite: true);
                    }
                    catch
                    {
                        // Best-effort only; a locked backup must not fail the write.
                    }
                }
                byte[] bytes = new UTF8Encoding(false).GetBytes(payload);
                using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    stream.Write(bytes, 0, bytes.Length);
                    stream.Flush(flushToDisk: true);
                }
                File.Move(temporary, path, overwrite: true);
                return true;
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

        private static string? ReadTextOrNull(string path)
        {
            try
            {
                return File.Exists(path) ? File.ReadAllText(path, Encoding.UTF8) : null;
            }
            catch
            {
                return null;
            }
        }
    }
}
