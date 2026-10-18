using System;
using System.IO;

namespace SmsWorkbench
{
    /// <summary>
    /// Resolves the Python interpreter used for backend commands.
    /// </summary>
    /// <remarks>
    /// The self-contained installer ships a pinned CPython under
    /// <c>&lt;root&gt;/runtime/python</c>. That interpreter is preferred when the
    /// operator has not configured a path (settings: <c>runtime.python_path</c>,
    /// default <c>python</c>), so a machine without Python on PATH still works.
    /// An explicit configured value always wins: an operator pointing the app at
    /// a development venv must not silently get the bundled one.
    /// </remarks>
    internal static class PythonPathResolver
    {
        /// <summary>Interpreter shipped by <c>scripts/stage_python_runtime.ps1</c>.</summary>
        internal static string BundledExecutable(IApplicationPaths paths)
        {
            string root = Path.Combine(paths.RootDirectory, "runtime", "python");
            return OperatingSystem.IsWindows()
                ? Path.Combine(root, "python.exe")
                : Path.Combine(root, "bin", "python3");
        }

        public static string Resolve(IApplicationPaths paths, string configured)
        {
            string value = (configured ?? "").Trim();
            if (value.Length == 0) value = "python";

            // Only the unconfigured default may be upgraded to the bundled
            // runtime. "py" and absolute paths are explicit operator choices.
            if (value == "python")
            {
                string bundled = BundledExecutable(paths);
                if (File.Exists(bundled)) return bundled;
            }

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
