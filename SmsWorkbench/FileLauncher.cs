namespace SmsWorkbench
{
    public interface IFileLauncher
    {
        bool Exists(string path);
        void Open(string path);
    }

    public sealed class FileLauncher : IFileLauncher
    {
        public bool Exists(string path) => File.Exists(path) || Directory.Exists(path);

        public void Open(string path)
        {
            if (!Exists(path)) return;
            Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
        }
    }
}
