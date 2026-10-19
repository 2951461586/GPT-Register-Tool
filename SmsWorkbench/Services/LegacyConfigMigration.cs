// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System;
using System.Collections.Generic;
using System.IO;

namespace SmsWorkbench
{
    /// <summary>
    /// Moves credential-bearing files out of the repository root into the
    /// per-user configuration directory. Mirrors <c>sms_tool.paths.migrate_legacy_root_files</c>
    /// so whichever side starts first leaves the other with nothing left to do.
    /// </summary>
    public static class LegacyConfigMigration
    {
        public static readonly IReadOnlyList<string> LegacyRootFiles = new[]
        {
            "config.json",
            "proxy.json",
            "runtime.json",
            "payment.json",
            "mailbox_tokens.txt",
        };

        /// <summary>
        /// Moves each legacy file (and its <c>.bak</c>) that the destination does
        /// not already have. An existing destination file always wins, so a re-run
        /// is a no-op. A failure on one file leaves it in place and never blocks
        /// the others. Returns the names that were moved.
        /// </summary>
        public static IReadOnlyList<string> Run(string sourceDirectory, string destinationDirectory)
        {
            var moved = new List<string>();
            try
            {
                if (string.Equals(
                        Path.GetFullPath(sourceDirectory).TrimEnd(Path.DirectorySeparatorChar),
                        Path.GetFullPath(destinationDirectory).TrimEnd(Path.DirectorySeparatorChar),
                        StringComparison.OrdinalIgnoreCase))
                {
                    return moved;
                }
            }
            catch (Exception)
            {
                return moved;
            }

            foreach (string name in LegacyRootFiles)
            {
                foreach (string candidate in new[] { name, name + ".bak" })
                {
                    string source = Path.Combine(sourceDirectory, candidate);
                    string target = Path.Combine(destinationDirectory, candidate);
                    if (!File.Exists(source) || File.Exists(target)) continue;
                    try
                    {
                        MoveFile(source, target);
                        moved.Add(candidate);
                    }
                    catch (Exception)
                    {
                        // Leave the file where it was; callers keep working from
                        // the new directory and the operator can retry.
                    }
                }
            }
            return moved;
        }

        private static void MoveFile(string source, string target)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            string staging = target + ".migrating." + Environment.ProcessId;
            try
            {
                File.Copy(source, staging, overwrite: true);
                if (new FileInfo(staging).Length != new FileInfo(source).Length)
                    throw new IOException("size mismatch while migrating " + Path.GetFileName(source));
                File.Move(staging, target, overwrite: true);
            }
            finally
            {
                if (File.Exists(staging)) File.Delete(staging);
            }
            File.Delete(source);
        }
    }
}
