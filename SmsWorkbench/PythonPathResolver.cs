#nullable enable

namespace SmsWorkbench
{
    internal static class PythonPathResolver
    {
        public static string Resolve(IApplicationPaths paths, string configured)
        {
            string value = (configured ?? "").Trim();
            if (value.Length == 0) value = "python";

            string expanded = Environment.ExpandEnvironmentVariables(value);
            if (Path.IsPathRooted(expanded)) return expanded;

            // Bare commands (python, py, etc.) must continue through PATH. A
            // configured path containing a directory component is resolved from
            // the repository root because ProcessStartInfo does not use its
            // WorkingDirectory when locating the executable.
            if (expanded.Contains(Path.DirectorySeparatorChar)
                || expanded.Contains(Path.AltDirectorySeparatorChar)
                || expanded.StartsWith('.'))
            {
                return Path.GetFullPath(Path.Combine(paths.RootDirectory, expanded));
            }

            return expanded;
        }
    }
}
