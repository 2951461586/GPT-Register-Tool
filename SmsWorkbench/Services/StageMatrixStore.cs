// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

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
        // On overflow we drop back to HALF the cap rather than to exactly
        // MaxRecords. Trimming to MaxRecords would rewrite the file on every
        // single append once the cap is reached (O(N) per append); trimming to
        // half makes it one rewrite per MaxRecords/2 appends, i.e. amortized
        // O(1), while the file still always holds between half and full cap.
        private const int TrimToRecords = MaxRecords / 2;
        private readonly string _path;
        private readonly object _sync = new();
        // Serialized lines kept in memory so Append never has to read the file
        // back. null means "not loaded yet" - the file is read lazily once, on
        // first Append or Load. Append is called straight from
        // StageMatrixViewModel.Apply (UI thread), so this used to put a full
        // File.ReadLines + rewrite of a ~1 MB file on every single progress
        // event: measured 24 ms per append once the store was at its cap.
        private List<string>? _lines;

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
                List<string> lines = EnsureLoaded();
                return lines.Count == 0
                    ? Array.Empty<BackendProgressEvent>()
                    : lines.Select(Parse).Where(value => value != null).Cast<BackendProgressEvent>().ToArray();
            }
        }

        public void Append(BackendProgressEvent value)
        {
            ArgumentNullException.ThrowIfNull(value);
            BackendProgressEvent persisted = value with { AccountRef = AccountReference(value.AccountRef) };
            string line = JsonSerializer.Serialize(persisted);
            lock (_sync)
            {
                List<string> lines = EnsureLoaded();
                File.AppendAllText(_path, line + Environment.NewLine);
                lines.Add(line);
                if (lines.Count <= MaxRecords) return;

                // Rare path (once per MaxRecords/2 appends). Re-read the file
                // instead of trusting the buffer: there is no single-instance
                // guard, so a second workbench process may have appended lines
                // this buffer has never seen, and rewriting straight from the
                // buffer would silently drop them.
                List<string> merged = File.Exists(_path)
                    ? File.ReadLines(_path).TakeLast(MaxRecords).ToList()
                    : lines;
                if (merged.Count > TrimToRecords) merged.RemoveRange(0, merged.Count - TrimToRecords);
                _lines = merged;
                File.WriteAllLines(_path, _lines);
            }
        }

        public void Clear()
        {
            lock (_sync)
            {
                _lines = new List<string>();
                if (File.Exists(_path)) File.Delete(_path);
            }
        }

        private List<string> EnsureLoaded()
        {
            if (_lines != null) return _lines;
            _lines = File.Exists(_path)
                ? File.ReadLines(_path).TakeLast(MaxRecords).ToList()
                : new List<string>();
            return _lines;
        }

        private static BackendProgressEvent? Parse(string line)
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
