using System.IO;

namespace SmsWorkbench
{
    public interface IApplicationPaths
    {
        string RootDirectory { get; }
        string BackendScriptPath { get; }
    }

    public sealed class ApplicationPaths : IApplicationPaths
    {
        public ApplicationPaths(string baseDirectory)
        {
            RootDirectory = FindRepositoryRoot(baseDirectory);
            BackendScriptPath = Path.Combine(RootDirectory, "chatgpt_phone_reg.py");
        }

        public string RootDirectory { get; }

        public string BackendScriptPath { get; }

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
