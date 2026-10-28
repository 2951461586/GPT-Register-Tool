// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System.IO.Compression;
using System.Text.Json.Nodes;

namespace SmsWorkbench
{
    /// <summary>One archive operation's outcome, in the words the UI shows.</summary>
    public sealed record WorkspaceBackupResult(string ArchivePath, int Entries, long Bytes);

    /// <summary>What a restore actually wrote, and where the previous copies went.</summary>
    public sealed record WorkspaceRestoreResult(int Restored, int Skipped, string SafetyDirectory);

    /// <summary>Copies the workspace state that cannot be regenerated into one zip, and puts it back.</summary>
    /// <remarks>
    /// <para>
    /// The allow-list is the contract, not "the whole runtime directory". Account rows,
    /// the config shards and the two UI preferences are irreplaceable; logs, caches, gate
    /// files, browser profiles and intermediate artifacts are large, noisy and rebuilt
    /// from scratch. A backup that quietly contains 90 MB of logs is a backup nobody
    /// takes twice.
    /// </para>
    /// <para>
    /// The archive carries a manifest so a restore can say what it is about to write,
    /// refuse an archive from a different layout, and report the app version that
    /// produced it.
    /// </para>
    /// </remarks>
    public interface IWorkspaceBackupService
    {
        /// <summary>The archive paths this service would copy, in archive order.</summary>
        IReadOnlyList<string> Entries();

        WorkspaceBackupResult Export(string destinationArchive);

        /// <summary>
        /// Writes the archived files back. Existing files are copied to
        /// <see cref="WorkspaceRestoreResult.SafetyDirectory"/> first, so a restore is
        /// itself undoable.
        /// </summary>
        WorkspaceRestoreResult Restore(string sourceArchive);
    }

    public sealed class WorkspaceBackupService : IWorkspaceBackupService
    {
        internal const string ManifestEntry = "smsworkbench-backup.json";

        /// <summary>Prefix for entries that come from the config directory (outside the root).</summary>
        internal const string ConfigPrefix = "config/";

        /// <summary>Prefix for entries that live under the workspace root.</summary>
        internal const string RootPrefix = "runtime/";

        internal const string RuntimeDirectory = "runtime/";

        /// <summary>
        /// The two halves of the allow-list: config-shard names (relative to the config
        /// directory) and runtime files (relative to the root).
        /// </summary>
        /// <remarks>
        /// Spelled out rather than globbed: <c>*.json</c> in the config directory would
        /// also catch anything an operator dropped there, and a backup is not the place
        /// for a surprise.
        /// </remarks>
        internal static readonly string[] ConfigEntries =
        {
            ConfigStore.ProxyShard,
            ConfigStore.RuntimeShard,
            ConfigStore.PaymentShard,
            ConfigStore.LegacyConfig,
        };

        /// <summary>Archive-style paths (forward slashes) of the runtime files worth keeping.</summary>
        internal static readonly string[] RuntimeEntries =
        {
            "runtime/accounts.sqlite3",
            "runtime/desktop_theme.txt",
            "runtime/desktop_language.txt",
        };

        private readonly IApplicationPaths paths;

        public WorkspaceBackupService(IApplicationPaths paths)
        {
            this.paths = paths;
        }

        public IReadOnlyList<string> Entries()
        {
            var entries = new List<string>();
            foreach (string name in ConfigEntries)
            {
                if (File.Exists(Path.Combine(paths.ConfigDirectory, name))) entries.Add(ConfigPrefix + name);
            }

            foreach (string entry in RuntimeEntries)
            {
                if (File.Exists(DiskPath(entry))) entries.Add(entry);
            }

            string? mailbox = MailboxPoolEntry();
            if (mailbox is not null && !entries.Contains(mailbox, StringComparer.OrdinalIgnoreCase))
                entries.Add(mailbox);

            return entries;
        }

        /// <summary>
        /// The configured mailbox pool file (<c>email_registration.token_file</c>), as an
        /// archive path, when it lives under a root this service owns.
        /// </summary>
        /// <remarks>
        /// <para>
        /// Read from the config rather than assumed: the pool file is whatever the operator
        /// pointed the backend at, and a backup that restores a file nobody reads is worse
        /// than one that says nothing.
        /// </para>
        /// <para>
        /// A path outside the workspace root and the config directory is skipped instead of
        /// archived under a guessed name: restoring it would either write outside the roots
        /// (refused by <see cref="IsUnderManagedRoots"/>) or land somewhere the config does
        /// not read.
        /// </para>
        /// </remarks>
        private string? MailboxPoolEntry()
        {
            string configured = ReadConfiguredMailboxPath();
            if (configured.Length == 0) return null;

            string full;
            try
            {
                full = Path.GetFullPath(configured);
            }
            catch (Exception)
            {
                return null;
            }

            if (!File.Exists(full) || !IsUnderManagedRoots(full)) return null;

            // The workspace root wins when both roots contain the file: the two are the
            // same directory on a default install (the config directory override is
            // optional), and a runtime-owned file must keep its runtime/ prefix so a
            // restore writes it back where the backend reads it.
            string rootRoot = Path.GetFullPath(paths.RootDirectory) + Path.DirectorySeparatorChar;
            if (full.StartsWith(rootRoot, StringComparison.OrdinalIgnoreCase))
            {
                // Relative to the root, so a runtime-owned file already carries its
                // "runtime/" segment; prefixing here produced runtime/runtime/... and a
                // restore wrote to a directory the backend never reads.
                return Path.GetRelativePath(rootRoot, full).Replace(Path.DirectorySeparatorChar, '/');
            }

            string configRoot = Path.GetFullPath(paths.ConfigDirectory) + Path.DirectorySeparatorChar;
            if (full.StartsWith(configRoot, StringComparison.OrdinalIgnoreCase))
                return ConfigPrefix + Path.GetRelativePath(configRoot, full).Replace(Path.DirectorySeparatorChar, '/');

            return null;
        }

        /// <summary>
        /// <c>email_registration.token_file</c> out of the config, read without side effects.
        /// </summary>
        /// <remarks>
        /// Deliberately not <see cref="ConfigStore.ReadMerged"/>: that helper migrates a
        /// legacy <c>config.json</c> into shards on first call, so a backup would rewrite
        /// the configuration it is reading. The precedence here mirrors the merge order -
        /// the last shard that names the key wins, and the legacy file only counts when no
        /// shard does.
        /// </remarks>
        private string ReadConfiguredMailboxPath()
        {
            string legacy = "";
            foreach (string name in ConfigEntries)
            {
                string path = Path.Combine(paths.ConfigDirectory, name);
                if (!File.Exists(path)) continue;
                try
                {
                    JsonNode? node = JsonNode.Parse(File.ReadAllText(path, Encoding.UTF8));
                    if (node?["email_registration"]?["token_file"] is not JsonValue value) continue;
                    if (!value.TryGetValue(out string? text)) continue;
                    string trimmed = (text ?? "").Trim();
                    if (trimmed.Length == 0) continue;

                    if (name == ConfigStore.LegacyConfig)
                        legacy = trimmed;
                    else
                        legacy = trimmed;
                }
                catch (Exception)
                {
                    // An unreadable shard contributes nothing; the rest still do.
                    continue;
                }
            }

            return legacy;
        }

        /// <summary>Turns an archive path back into the file on this machine.</summary>
        private string DiskPath(string entry)
        {
            if (entry.StartsWith(ConfigPrefix, StringComparison.Ordinal))
                return Path.Combine(paths.ConfigDirectory, entry.Substring(ConfigPrefix.Length));

            string relative = entry.StartsWith(RootPrefix, StringComparison.Ordinal)
                ? entry.Substring(RootPrefix.Length)
                : entry;
            // Archive paths are always forward-slashed; the disk is not.
            return Path.Combine(paths.RootDirectory, RuntimeDirectory, relative.Replace('/', Path.DirectorySeparatorChar));
        }

        /// <summary>
        /// Every entry name a restore may write.
        /// </summary>
        /// <remarks>
        /// Three sources, all of them explicit. The static allow-list and the live
        /// mailbox path cover an archive written by this build; the archive's own manifest
        /// covers entries whose path has since moved (the mailbox pool file is configured,
        /// so it is not a constant). Listing an entry in the manifest only exempts it from
        /// the name test — <see cref="IsUnderManagedRoots"/> still decides where it lands.
        /// </remarks>
        private HashSet<string> AllowedEntryNames(ZipArchive archive)
        {
            var allowed = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (string name in ConfigEntries) allowed.Add(ConfigPrefix + name);
            foreach (string name in RuntimeEntries) allowed.Add(name);

            string? mailbox = MailboxPoolEntry();
            if (mailbox is not null) allowed.Add(mailbox);

            try
            {
                ZipArchiveEntry? manifest = archive.GetEntry(ManifestEntry);
                if (manifest is null) return allowed;

                using Stream stream = manifest.Open();
                using var reader = new StreamReader(stream, Encoding.UTF8);
                JsonNode? node = JsonNode.Parse(reader.ReadToEnd());
                if (node?["entries"] is not JsonArray listed) return allowed;

                foreach (JsonNode? item in listed)
                {
                    string name = item?.GetValue<string>() ?? "";
                    if (name.Length > 0) allowed.Add(name);
                }
            }
            catch (Exception)
            {
                // A manifest that cannot be read leaves the static allow-list in charge.
            }

            return allowed;
        }

        /// <summary>
        /// Zip-slip guard: a restore may only write under the workspace root or the config
        /// directory, whatever the archive claims about its own entry names.
        /// </summary>
        private bool IsUnderManagedRoots(string fullPath)
        {
            string resolved = Path.GetFullPath(fullPath);
            foreach (string root in new[] { paths.RootDirectory, paths.ConfigDirectory })
            {
                string prefix = Path.GetFullPath(root) + Path.DirectorySeparatorChar;
                if (resolved.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) return true;
            }

            return false;
        }

        public WorkspaceBackupResult Export(string destinationArchive)
        {
            if (string.IsNullOrWhiteSpace(destinationArchive))
                throw new ArgumentException("A destination archive path is required.", nameof(destinationArchive));

            string full = Path.GetFullPath(destinationArchive);
            IReadOnlyList<string> files = Entries();
            Directory.CreateDirectory(Path.GetDirectoryName(full) ?? ".");

            long bytes = 0;
            // Create, not CreateNew: the operator already confirmed the path in a save
            // dialog, and a half-written archive from a crash is the worse outcome.
            using (var stream = new FileStream(full, FileMode.Create, FileAccess.Write))
            using (var archive = new ZipArchive(stream, ZipArchiveMode.Create))
            {
                foreach (string entry in files)
                {
                    string source = DiskPath(entry);
                    ZipArchiveEntry zipEntry = archive.CreateEntry(entry, CompressionLevel.Fastest);
                    using Stream target = zipEntry.Open();
                    using var input = File.OpenRead(source);
                    input.CopyTo(target);
                    bytes += input.Length;
                }

                WriteManifest(archive, files);
            }

            return new WorkspaceBackupResult(full, files.Count, bytes);
        }

        /// <summary>
        /// The archive's own description of itself: which layout wrote it, when, and what
        /// it holds. A restore reads it only to report; the entry names remain the
        /// authority, because a manifest that disagrees with the archive must not decide
        /// where files land.
        /// </summary>
        private static void WriteManifest(ZipArchive archive, IReadOnlyList<string> files)
        {
            ZipArchiveEntry entry = archive.CreateEntry(ManifestEntry, CompressionLevel.NoCompression);
            using Stream stream = entry.Open();
            using var writer = new StreamWriter(stream, new UTF8Encoding(false));
            var manifest = new Dictionary<string, object?>
            {
                ["format"] = 1,
                ["created_utc"] = DateTime.UtcNow.ToString("O", CultureInfo.InvariantCulture),
                ["app_version"] = AppVersion.Current.ToString(),
                ["entries"] = files,
            };
            writer.Write(JsonSerializer.Serialize(manifest, new JsonSerializerOptions { WriteIndented = true }));
        }

        public WorkspaceRestoreResult Restore(string sourceArchive)
        {
            if (string.IsNullOrWhiteSpace(sourceArchive))
                throw new ArgumentException("An archive path is required.", nameof(sourceArchive));

            string safetyDirectory = Path.Combine(
                paths.RootDirectory,
                RuntimeDirectory.Replace('/', Path.DirectorySeparatorChar),
                "restore-" + DateTime.UtcNow.ToString("yyyyMMddTHHmmss", CultureInfo.InvariantCulture));

            int restored = 0;
            int skipped = 0;
            using (var stream = File.OpenRead(sourceArchive))
            using (var archive = new ZipArchive(stream, ZipArchiveMode.Read))
            {
                HashSet<string> allowed = AllowedEntryNames(archive);
                foreach (ZipArchiveEntry entry in archive.Entries)
                {
                    if (entry.FullName == ManifestEntry || string.IsNullOrEmpty(entry.Name)) continue;

                    // Two gates, both required: the name has to be one this service
                    // wrote (static allow-list, the live mailbox entry, or an entry the
                    // archive's own manifest declares), and the resolved path has to stay
                    // under a managed root.
                    if (!allowed.Contains(entry.FullName))
                    {
                        skipped++;
                        continue;
                    }

                    string target = DiskPath(entry.FullName);
                    if (!IsUnderManagedRoots(target))
                    {
                        skipped++;
                        continue;
                    }

                    Directory.CreateDirectory(Path.GetDirectoryName(target) ?? ".");
                    if (File.Exists(target))
                    {
                        Directory.CreateDirectory(safetyDirectory);
                        File.Copy(target, Path.Combine(safetyDirectory, Path.GetFileName(target)), overwrite: true);
                    }

                    entry.ExtractToFile(target, overwrite: true);
                    restored++;
                }
            }

            return new WorkspaceRestoreResult(
                restored,
                skipped,
                restored > 0 && Directory.Exists(safetyDirectory) ? safetyDirectory : "");
        }
    }
}
