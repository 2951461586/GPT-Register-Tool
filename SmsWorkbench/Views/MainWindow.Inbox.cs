using SmsWorkbench.Localization;

namespace SmsWorkbench.Views
{
    public partial class MainWindow
    {
        // Inbox view and inline mail detail (modal so closing never drops MainWindow behind other windows)
        private void ShowInboxDialog(PoolRow row)
        {
            if (row == null) return;
            using var dialogCts = CancellationTokenSource.CreateLinkedTokenSource(_lifetimeCts.Token);
            InboxDialogWindow? dialog = null;

            async Task LoadEmails(CancellationToken token = default)
            {
                if (dialog == null || token.IsCancellationRequested) return;
                if (IsCfWorkerRow(row) || IsReMailRow(row) || IsSmailrRow(row))
                {
                    dialog.SetHeaderText(IsReMailRow(row) ? Loc.T("正在获取 ReMail 邮件...") : IsSmailrRow(row) ? Loc.T("正在获取 Smailr 邮件...") : Loc.T("正在获取 CFWorker 邮件..."));
                    try
                    {
                        dialog.MailItems.Clear();
                        foreach (MailItem item in await FetchBackendInbox(row, 25, token))
                        {
                            if (token.IsCancellationRequested) return;
                            dialog.MailItems.Add(item);
                        }
                        dialog.SetHeaderText(Loc.F("{0} - 最近 {1} 封邮件", row.Identifier, dialog.MailItems.Count));
                    }
                    catch (OperationCanceledException)
                    {
                        // Dialog closed while inbox request was still in flight.
                    }
                    catch (Exception ex)
                    {
                        dialog.SetHeaderText(Loc.T("获取邮件失败：") + ex.Message);
                        Log((IsReMailRow(row) ? "ReMail" : IsSmailrRow(row) ? "Smailr" : "CFWorker") + Loc.T("收件箱获取失败：") + ex.Message);
                    }
                    return;
                }

                dialog.SetHeaderText(Loc.T("正在刷新令牌..."));
                try
                {
                    dialog.MailItems.Clear();
                    foreach (MailItem item in await FetchBackendInbox(row, 20, token))
                    {
                        if (token.IsCancellationRequested) return;
                        dialog.MailItems.Add(item);
                    }
                    dialog.SetHeaderText(Loc.F("{0} - 最近 {1} 封邮件", row.Identifier, dialog.MailItems.Count));
                }
                catch (OperationCanceledException)
                {
                    // Dialog closed while inbox request was still in flight.
                }
                catch (Exception ex)
                {
                    dialog.SetHeaderText(Loc.T("加载失败：") + ex.Message);
                    Log(Loc.T("收件箱加载异常：") + ex.Message);
                }
            }

            dialog = new InboxDialogWindow(
                this,
                row.Identifier,
                () => RunUiTask(() => LoadEmails(dialogCts.Token)),
                Log);
            dialog.Loaded += (_, _) => RunUiTask(() => LoadEmails(dialogCts.Token));
            dialog.Closed += (_, _) => dialogCts.Cancel();
            dialog.ShowDialog();
        }

        private async Task<List<MailItem>> FetchBackendInbox(PoolRow row, int limit, CancellationToken ct = default)
        {
            string remailToken = IsReMailRow(row) ? (row.MailboxToken ?? "").Trim() : "";
            string mailboxLine = await FindMailboxLineForRowAsync(row, ct).ConfigureAwait(true);
            if (mailboxLine.Length == 0 && BackendCommandPlanner.MailboxArgumentForLine(row.RawLine).Length > 0)
            {
                mailboxLine = row.RawLine;
            }
            // The planner owns the flag vocabulary, the temp mailbox file, the
            // session file, the proxy route, the ReMail environment and the
            // timeout; this partial only supplies the resolved values.
            BackendCommandPlan plan = BackendCommandPlanner.CreateViewInbox(
                row.Identifier,
                limit,
                BackendCommandPlanner.MailboxArgumentForLine(mailboxLine),
                mailboxLine,
                SessionFileFor(row),
                GetMailboxProxy(),
                remailToken);
            try
            {
                BackendCommandResult result = await backendClient.RunAsync(
                    BackendCommand.Create(
                        plan.TaskName,
                        plan.Arguments,
                        plan.TimeoutMilliseconds ?? 120000,
                        plan.Environment),
                    cancellationToken: ct);
                if (!result.Payload.HasValue)
                    throw new InvalidOperationException(InboxReader.FailureMessage(result));
                JsonElement payload = result.Payload.Value;
                if (!payload.TryGetProperty("ok", out JsonElement ok) || !ok.GetBoolean())
                {
                    string error = JsonString(payload, "error");
                    throw new InvalidOperationException(error.Length > 0 ? error : InboxReader.FailureMessage(result));
                }
                var items = new List<MailItem>();
                if (payload.TryGetProperty("messages", out JsonElement messages) && messages.ValueKind == JsonValueKind.Array)
                {
                    foreach (JsonElement msg in messages.EnumerateArray())
                    {
                        string received = JsonString(msg, "receivedDateTime");
                        if (received.Length > 19) received = received.Substring(0, 19).Replace("T", " ");
                        items.Add(new MailItem
                        {
                            ReceivedAt = received,
                            From = JsonString(msg, "from"),
                            Subject = JsonString(msg, "subject"),
                            BodyPreview = JsonString(msg, "bodyPreview"),
                            Body = JsonString(msg, "body"),
                            VerificationCode = JsonString(msg, "verificationCode")
                        });
                    }
                }
                return items;
            }
            finally
            {
                foreach (string tempFile in plan.TempFiles)
                    TryDeleteFile(tempFile);
            }
        }

        // The three provider predicates live in MailboxRowClassifier: the
        // registration request builder takes IsCfWorker as its decision, so they were
        // never page-local, and `RegisterViewModel.LocalUnregisteredVerdict` needs them
        // too. These three wrappers keep the partial's own names so call sites did not
        // move with the implementation.
        private bool IsCfWorkerRow(PoolRow row) => MailboxRowClassifier.IsCfWorker(row);

        private bool IsReMailRow(PoolRow row) => MailboxRowClassifier.IsReMail(row);

        private bool IsSmailrRow(PoolRow row) => MailboxRowClassifier.IsSmailr(row);
    }
}
