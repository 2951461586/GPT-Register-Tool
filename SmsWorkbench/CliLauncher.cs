using System.Diagnostics;
using System.Text;

namespace SmsWorkbench
{
    /// <summary>
    /// Service for launching the Python CLI backend (chatgpt_phone_reg.py).
    /// Centralizes ProcessStartInfo construction, script path resolution,
    /// and output capture to eliminate duplication across MainWindow partials.
    /// </summary>
    public static class CliLauncher
    {
        /// <summary>
        /// Resolve the full path to the backend script.
        /// Returns empty string if not found.
        /// </summary>
        public static string ResolveScript(string rootDir)
        {
            string script = Path.Combine(rootDir, "chatgpt_phone_reg.py");
            return File.Exists(script) ? script : "";
        }

        /// <summary>
        /// Build a ProcessStartInfo for the Python CLI with UTF-8 output capture.
        /// </summary>
        public static ProcessStartInfo CreateStartInfo(
            string rootDir,
            string scriptPath,
            List<string> args,
            Func<string, string> quoteFunc,
            Func<List<string>, string> joinArgsFunc)
        {
            return new ProcessStartInfo
            {
                FileName = "python",
                Arguments = quoteFunc(scriptPath) + " " + joinArgsFunc(args),
                WorkingDirectory = rootDir,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
        }

        /// <summary>
        /// Run the CLI synchronously with a timeout and return (stdout, stderr, exitCode).
        /// </summary>
        public static (string stdout, string stderr, int exitCode) RunSync(
            string rootDir,
            List<string> args,
            Func<string, string> quoteFunc,
            Func<List<string>, string> joinArgsFunc,
            int timeoutMs = 120000)
        {
            string script = ResolveScript(rootDir);
            if (script.Length == 0)
                throw new FileNotFoundException("Backend script not found", "chatgpt_phone_reg.py");

            var psi = CreateStartInfo(rootDir, script, args, quoteFunc, joinArgsFunc);
            var output = new StringBuilder();
            var error = new StringBuilder();

            using (var process = new Process { StartInfo = psi })
            {
                process.OutputDataReceived += (_, ev) => { if (ev.Data != null) { lock (output) output.AppendLine(ev.Data); } };
                process.ErrorDataReceived += (_, ev) => { if (ev.Data != null) { lock (error) error.AppendLine(ev.Data); } };

                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                process.WaitForExit(timeoutMs);

                if (!process.HasExited)
                {
                    try
                    {
                        process.Kill(entireProcessTree: true);
                        process.WaitForExit();
                    }
                    catch { }
                    throw new TimeoutException($"Backend execution timed out ({timeoutMs / 1000}s)");
                }

                string stdout;
                string stderr;
                lock (output) stdout = output.ToString().Trim();
                lock (error) stderr = error.ToString().Trim();
                return (stdout, stderr, process.ExitCode);
            }
        }

        /// <summary>
        /// Create and start a process for async/event-driven use.
        /// The caller is responsible for attaching event handlers and calling
        /// BeginOutputReadLine/BeginErrorReadLine.
        /// </summary>
        public static Process CreateAndStart(
            string rootDir,
            List<string> args,
            Func<string, string> quoteFunc,
            Func<List<string>, string> joinArgsFunc)
        {
            string script = ResolveScript(rootDir);
            if (script.Length == 0)
                throw new FileNotFoundException("Backend script not found", "chatgpt_phone_reg.py");

            var psi = CreateStartInfo(rootDir, script, args, quoteFunc, joinArgsFunc);
            var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
            return process;
        }
    }
}
