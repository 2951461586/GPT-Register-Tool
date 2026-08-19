using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace SmsWorkbench
{
    public interface IStageMatrixStore
    {
        IReadOnlyList<BackendProgressEvent> Load();
        void Append(BackendProgressEvent value);
        void Clear();
    }

    public sealed class JsonlStageMatrixStore : IStageMatrixStore
    {
        private const int MaxRecords = 2000;
        private readonly string _path;
        private readonly object _sync = new();

        public JsonlStageMatrixStore(IApplicationPaths paths)
        {
            string directory = Path.Combine(paths.RootDirectory, "runtime");
            Directory.CreateDirectory(directory);
            _path = Path.Combine(directory, "stage_matrix.jsonl");
        }

        public IReadOnlyList<BackendProgressEvent> Load()
        {
            lock (_sync)
            {
                if (!File.Exists(_path)) return Array.Empty<BackendProgressEvent>();
                return File.ReadLines(_path).TakeLast(MaxRecords)
                    .Select(Parse).Where(value => value != null).Cast<BackendProgressEvent>().ToArray();
            }
        }

        public void Append(BackendProgressEvent value)
        {
            ArgumentNullException.ThrowIfNull(value);
            BackendProgressEvent persisted = value with { AccountRef = AccountReference(value.AccountRef) };
            lock (_sync)
            {
                File.AppendAllText(_path, JsonSerializer.Serialize(persisted) + Environment.NewLine);
                string[] lines = File.ReadLines(_path).TakeLast(MaxRecords + 1).ToArray();
                if (lines.Length <= MaxRecords) return;
                string temporary = _path + ".tmp";
                File.WriteAllLines(temporary, lines.TakeLast(MaxRecords));
                File.Move(temporary, _path, true);
            }
        }

        public void Clear()
        {
            lock (_sync)
            {
                if (File.Exists(_path)) File.Delete(_path);
            }
        }

        private static BackendProgressEvent Parse(string line)
        {
            try { return JsonSerializer.Deserialize<BackendProgressEvent>(line); }
            catch (JsonException) { return null; }
        }

        private static string AccountReference(string value)
        {
            string text = value?.Trim() ?? "";
            if (text.Length == 0) return "";
            byte[] digest = SHA256.HashData(Encoding.UTF8.GetBytes(text));
            return "account-" + Convert.ToHexString(digest)[..12].ToLowerInvariant();
        }
    }
}
