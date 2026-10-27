// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// Registration form state (the batch count) and the pure rules behind the
    /// register / scan flows: worker defaults, number parsing, which rows still
    /// need registering and which mailbox argument a batch uses. Window-independent
    /// so those rules are xunit-testable (placement rule 8); dialogs and backend
    /// calls stay in MainWindow.Register.cs.
    /// </summary>
    public sealed class RegisterViewModel : ObservableObject
    {
        private string countText = "1";

        public string CountText
        {
            get => countText;
            set => SetProperty(ref countText, value ?? "1");
        }

        private string chataiMailboxFilePath = "";

        /// <summary>Mailbox file chosen by the last import; the pool reload reads it back.</summary>
        public string ChataiMailboxFilePath
        {
            get => chataiMailboxFilePath;
            set => SetProperty(ref chataiMailboxFilePath, value ?? "");
        }

        public int CountValue()
        {
            return int.TryParse(CountText, out int value) && value > 0 ? value : 1;
        }

        public int DefaultWorkerCount()
        {
            return Math.Max(1, Math.Min(8, CountValue()));
        }

        public static int ParsePositiveInt(string text, int min, int max, int fallback)
        {
            if (!int.TryParse((text ?? "").Trim(), out int value)) return fallback;
            return Math.Max(min, Math.Min(max, value));
        }

        public static bool HasRegisteredAccountState(PoolRow row)
        {
            string status = row.Status ?? "";
            // ``Loc.DisplayContains``, not ``status.Contains("已注册")``: the row's
            // text is rendered through ``Loc.T``, so comparing the Chinese
            // literal made this predicate return false for every row once the
            // UI language was English -- and the caller then offered an
            // already-registered mailbox for registration again.
            return Loc.DisplayContains(status, "已注册")
                || Loc.DisplayContains(status, "支付完成")
                || Loc.DisplayContains(status, "已导入")
                || status.Contains("PayPal", StringComparison.OrdinalIgnoreCase);
        }

        /// <summary>
        /// Decides from local fields alone whether a row still needs registering.
        /// Null means every local field was empty, so only a backend lookup of the
        /// mailbox line can tell.
        /// </summary>
        public static bool? LocalUnregisteredVerdict(PoolRow? row, bool isCfWorker)
        {
            if (row == null) return false;
            if (RegistrationStatusPresentation.IsPartial(row.RegistrationStatus)
                || RegistrationStatusPresentation.IsPartial(row.Status)) return false;
            if (HasRegisteredAccountState(row)) return false;
            if (isCfWorker) return true;
            if (!string.IsNullOrWhiteSpace(row.MailboxLine)) return true;
            if (!string.IsNullOrWhiteSpace(row.RawRefreshToken)) return true;
            if (!string.IsNullOrWhiteSpace(row.RawLine) && BackendCommandPlanner.MailboxArgumentForLine(row.RawLine).Length > 0) return true;
            return null;
        }

        /// <summary>
        /// One provider's argument when the batch is homogeneous; otherwise the
        /// legacy parser, which is the compatibility superset for mixed selections.
        /// </summary>
        public static string MailboxArgument(ISet<string> argumentsInBatch)
        {
            return argumentsInBatch.Count == 1 ? argumentsInBatch.First() : "--chatai-mailbox-file";
        }
    }
}
