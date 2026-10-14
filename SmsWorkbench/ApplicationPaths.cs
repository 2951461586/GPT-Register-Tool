// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System;
using System.IO;

namespace SmsWorkbench
{
    public interface IApplicationPaths
    {
        string RootDirectory { get; }

        /// <summary>
        /// Directory holding the config shards and the default mailbox pool.
        /// Lives outside the repository so credentials can never be committed,
        /// cleaned away by <c>git clean</c>, or lost with a re-clone.
        /// </summary>
        string ConfigDirectory { get; }

        string BackendScriptPath { get; }
    }

    public sealed class ApplicationPaths : IApplicationPaths
    {
        /// <summary>Same name the Python backend reads (<c>sms_tool.paths.CONFIG_DIR_ENV</c>).</summary>
        public const string ConfigDirectoryEnvironmentVariable = "GPT_REGISTER_CONFIG_DIR";

        public ApplicationPaths(string baseDirectory, string? configDirectoryOverride = null)
        {
            RootDirectory = FindRepositoryRoot(baseDirectory);
            BackendScriptPath = Path.Combine(RootDirectory, "chatgpt_phone_reg.py");

            string? explicitDirectory = configDirectoryOverride;
            if (string.IsNullOrWhiteSpace(explicitDirectory))
                explicitDirectory = Environment.GetEnvironmentVariable(ConfigDirectoryEnvironmentVariable);

            if (!string.IsNullOrWhiteSpace(explicitDirectory))
            {
                ConfigDirectory = Path.GetFullPath(Environment.ExpandEnvironmentVariables(explicitDirectory.Trim()));
            }
            else
            {
                ConfigDirectory = DefaultConfigDirectory();
                // Only the implicit per-user directory pulls files out of the
                // checkout; an explicit override is an operator decision.
                LegacyConfigMigration.Run(RootDirectory, ConfigDirectory);
            }
        }

        public string RootDirectory { get; }

        public string ConfigDirectory { get; }

        public string BackendScriptPath { get; }

        public static string DefaultConfigDirectory()
        {
            string baseDirectory = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            if (string.IsNullOrWhiteSpace(baseDirectory))
                baseDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "AppData", "Local");
            return Path.Combine(baseDirectory, "GPTRegisterTool", "config");
        }

        private static string FindRepositoryRoot(string baseDirectory)
        {
            var current = new DirectoryInfo(Path.GetFullPath(baseDirectory));
            for (var depth = 0; current != null && depth < 10; depth++, current = current.Parent)
            {
                if (File.Exists(Path.Combine(current.FullName, "chatgpt_phone_reg.py")))
                    return current.FullName;
            }

            return Path.GetFullPath(baseDirectory);
        }
    }
}
