namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // ── Search clear button ──

        private void SearchClear_Click(object sender, RoutedEventArgs e)
        {
            SearchText = "";
            UpdateSearchClearVisibility();
        }

        /// <summary>
        /// Toggle the visibility of the search clear (×) button based on
        /// whether the search text is non-empty. Called from the SearchText
        /// setter and from the clear button click handler.
        /// </summary>
        private void UpdateSearchClearVisibility()
        {
            if (SearchClearButton != null)
            {
                SearchClearButton.Visibility = string.IsNullOrEmpty(SearchText)
                    ? Visibility.Collapsed
                    : Visibility.Visible;
            }
        }

        // ── DataGrid context menu handlers ──

        private void CtxViewDetail_Click(object sender, RoutedEventArgs e)
        {
            if (AccountGrid?.SelectedItem is PoolRow row)
                ShowAccountDetail(row);
        }

        private void CtxViewInbox_Click(object sender, RoutedEventArgs e)
        {
            if (AccountGrid?.SelectedItem is PoolRow row)
                ShowInboxDialog(row);
        }

        private void CtxCopyEmail_Click(object sender, RoutedEventArgs e)
        {
            if (AccountGrid?.SelectedItem is PoolRow row && !string.IsNullOrWhiteSpace(row.Identifier))
            {
                try
                {
                    Clipboard.SetText(row.Identifier);
                    NotifyInfo("邮箱已复制：" + row.Identifier);
                }
                catch (Exception ex)
                {
                    Log("复制邮箱失败：" + ex.Message);
                }
            }
        }

        private void CtxCopyAccessToken_Click(object sender, RoutedEventArgs e)
            => RunUiTask(() => CtxCopyAccessTokenAsync());

        private async Task CtxCopyAccessTokenAsync(CancellationToken ct = default)
        {
            if (AccountGrid?.SelectedItem is not PoolRow row)
            {
                NotifyWarning("请先选择一个账号。");
                return;
            }
            string accessToken = await ResolveAccountAccessTokenAsync(row);
            if (string.IsNullOrWhiteSpace(accessToken))
            {
                NotifyWarning("当前选中账号没有可复制的 AT。");
                return;
            }
            try
            {
                Clipboard.SetText(accessToken);
                NotifyInfo("AT 已复制。");
            }
            catch (Exception ex)
            {
                Log("复制 AT 失败：" + ex.Message);
            }
        }

        private void CtxCopyPayPal_Click(object sender, RoutedEventArgs e)
        {
            if (AccountGrid?.SelectedItem is PoolRow row && !string.IsNullOrWhiteSpace(row.PayPalUrl))
            {
                CopyPayPalUrl(row.PayPalUrl, row.Identifier);
            }
            else
            {
                NotifyWarning("当前选中行无支付链接。");
            }
        }

        private void CtxOpenPayPal_Click(object sender, RoutedEventArgs e)
        {
            if (AccountGrid?.SelectedItem is PoolRow row && !string.IsNullOrWhiteSpace(row.PayPalUrl))
            {
                OpenPayPalUrl(row.PayPalUrl, row.Identifier);
            }
            else
            {
                NotifyWarning("当前选中行无支付链接。");
            }
        }

        private void CtxOpenSource_Click(object sender, RoutedEventArgs e)
        {
            if (AccountGrid?.SelectedItem is PoolRow row)
                OpenAccountJson(row);
        }

        private void CtxCheckAccountAlive_Click(object sender, RoutedEventArgs e)
            => RunUiTask(() => CtxCheckAccountAliveAsync());

        private async Task CtxCheckAccountAliveAsync(CancellationToken ct = default)
        {
            if (AccountGrid?.SelectedItem is not PoolRow row || string.IsNullOrWhiteSpace(row.Identifier))
            {
                NotifyWarning("请先选择一个账号。");
                return;
            }
            await CheckAccountAliveAsync(row);
        }

        private void CtxBatchProtocolPayment_Click(object sender, RoutedEventArgs e)
        {
            BatchProtocolPayment_Click(sender, e);
        }

        private void CtxChangeEmail_Click(object sender, RoutedEventArgs e)
            => RunUiTask(() => CtxChangeEmailAsync());

        private void ChangeEmail_Click(object sender, RoutedEventArgs e)
            => RunUiTask(() => CtxChangeEmailAsync());

        private async Task CtxChangeEmailAsync(CancellationToken ct = default)
        {
            var rows = SelectedRowsOrCurrent().Where(row => row != null && !string.IsNullOrWhiteSpace(row.Identifier)).ToList();
            if (rows.Count == 0)
            {
                NotifyWarning("请先选择要换绑的账号。");
                return;
            }
            var options = ChangeEmailDialogService.Show(
                this,
                rows.Count,
                DefaultWorkerCount(),
                GetConfiguredSmailrDomain(),
                GetConfiguredCfWorkerDomain());
            if (options is null) return;
            var plan = BackendCommandPlanner.CreateChangeEmail(
                rows.Select(row => row.Identifier).Distinct(StringComparer.OrdinalIgnoreCase).ToList(),
                options.Provider,
                options.MailboxFile,
                options.Workers,
                options.SmailrDomain,
                options.CfworkerDomain,
                GetRegistrationProxyPool(),
                rootDir);
            string json;
            try { json = await RunBackendWithResultAsync(plan.TaskName, plan.Arguments.ToList(), plan.TimeoutMilliseconds ?? 900000); }
            finally { foreach (string path in plan.TempFiles) TryDeleteFile(path); }
            try
            {
                using var doc = JsonDocument.Parse(json);
                bool ok = doc.RootElement.TryGetProperty("ok", out var okEl) && okEl.GetBoolean();
                await DialogFactory.ShowInfoAsync(this, "邮箱换绑", ok ? "邮箱换绑完成。" : "邮箱换绑部分失败，请查看任务结果。 ");
                RefreshPools();
            }
            catch
            {
                await DialogFactory.ShowInfoAsync(this, "邮箱换绑", "未收到有效结果，请查看运行日志。");
            }
        }

        private async Task CheckAccountAliveAsync(PoolRow row, CancellationToken ct = default)
        {
            if (row == null || string.IsNullOrWhiteSpace(row.Identifier))
            {
                NotifyWarning("请先选择一个账号。");
                return;
            }

            if (!row.HasAccessToken)
            {
                await DialogFactory.ShowInfoAsync(this, "账号测活", "该账号未获取 Access Token，无法测活。请先登录获取 AT。");
                return;
            }

            try
            {
                Log($"正在进行账号测活：{row.Identifier}");
                var args = new List<string> { "--quota-usage", "--email", row.Identifier, "--refresh-timeout", "45" };
                AddRegistrationProxy(args);
                string json = await RunBackendWithResultAsync("账号测活", args);

                if (string.IsNullOrWhiteSpace(json))
                {
                    await DialogFactory.ShowInfoAsync(this, "账号测活", "账号测活失败：未收到有效响应。");
                    return;
                }

                using var doc = JsonDocument.Parse(json);
                var root = doc.RootElement;

                if (root.TryGetProperty("ok", out var okEl) && okEl.GetBoolean())
                {
                    string detail = FormatAccountLivenessDetail(root);
                    await DialogFactory.ShowInfoAsync(this, $"账号测活：{row.Identifier}", detail);
                    Log($"账号测活成功：{row.Identifier} → AT 有效");
                    RefreshPools();
                }
                else
                {
                    string error = root.TryGetProperty("error", out var errEl) ? errEl.GetString() ?? "未知错误" : "未知错误";
                    string status = root.TryGetProperty("status", out var stEl) ? stEl.GetString() ?? "" : "";
                    string msg = $"测活失败：{error}";
                    if (status == "token_invalid")
                        msg += "\n\n接口返回 HTTP 401，当前 Access Token 已失效。";
                    await DialogFactory.ShowInfoAsync(this, $"账号测活：{row.Identifier}", msg);
                    Log($"账号测活失败：{row.Identifier} → {error}");
                }
            }
            catch (Exception ex)
            {
                Log($"账号测活异常：{ex.Message}");
                await DialogFactory.ShowInfoAsync(this, "账号测活", $"测活异常：{ex.Message}");
            }
        }

        private static string FormatAccountLivenessDetail(JsonElement root)
        {
            var sb = new StringBuilder();
            string statusCode = root.TryGetProperty("status_code", out var codeEl) ? codeEl.ToString() : "";
            sb.AppendLine("状态：AT 有效");
            sb.AppendLine("接口：HTTP " + (string.IsNullOrWhiteSpace(statusCode) ? "200" : statusCode));
            return sb.ToString().TrimEnd();
        }
    }
}
