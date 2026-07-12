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

        private void CtxCopyPayPal_Click(object sender, RoutedEventArgs e)
        {
            if (AccountGrid?.SelectedItem is PoolRow row && !string.IsNullOrWhiteSpace(row.PayPalUrl))
            {
                CopyPayPalUrl(row.PayPalUrl);
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

        private void CtxMarkPayPal_Click(object sender, RoutedEventArgs e)
        {
            if (AccountGrid?.SelectedItem is PoolRow row)
                MarkPayPalComplete(row);
        }

        private async void CtxRefreshQuota_Click(object sender, RoutedEventArgs e)
        {
            if (AccountGrid?.SelectedItem is not PoolRow row || string.IsNullOrWhiteSpace(row.Identifier))
            {
                NotifyWarning("请先选择一个账号。");
                return;
            }
            await RefreshQuotaForRowAsync(row);
        }

        private async Task RefreshQuotaForRowAsync(PoolRow row)
        {
            if (row == null || string.IsNullOrWhiteSpace(row.Identifier))
            {
                NotifyWarning("请先选择一个账号。");
                return;
            }

            if (!row.HasAccessToken)
            {
                await DialogFactory.ShowInfoAsync(this, "刷新额度", "该账号没有 Access Token，无法查询额度。请先登录获取 AT。");
                return;
            }

            try
            {
                Log($"正在查询额度：{row.Identifier}");
                var args = new List<string> { "--quota-usage", "--email", row.Identifier, "--refresh-timeout", "45" };
                AddProxy(args);
                string json = await Task.Run(() => RunBackendWithResult("查询额度", args));

                if (string.IsNullOrWhiteSpace(json))
                {
                    await DialogFactory.ShowInfoAsync(this, "刷新额度", "查询额度失败：未收到有效响应。");
                    return;
                }

                using var doc = JsonDocument.Parse(json);
                var root = doc.RootElement;

                if (root.TryGetProperty("ok", out var okEl) && okEl.GetBoolean())
                {
                    string quotaStatus = root.TryGetProperty("quota_status", out var qsEl) ? qsEl.GetString() ?? "" : "";
                    string detail = FormatQuotaDetail(root);
                    await DialogFactory.ShowInfoAsync(this, $"额度查询：{row.Identifier}", detail);
                    Log($"额度查询成功：{row.Identifier} → {quotaStatus}");
                    RefreshPools();
                }
                else
                {
                    string error = root.TryGetProperty("error", out var errEl) ? errEl.GetString() ?? "未知错误" : "未知错误";
                    string status = root.TryGetProperty("status", out var stEl) ? stEl.GetString() ?? "" : "";
                    string msg = $"查询失败：{error}";
                    if (status == "token_invalid")
                        msg += "\n\nAccess Token 已失效，请先执行额度查询或 relogin 刷新 AT。";
                    await DialogFactory.ShowInfoAsync(this, $"额度查询：{row.Identifier}", msg);
                    Log($"额度查询失败：{row.Identifier} → {error}");
                }
            }
            catch (Exception ex)
            {
                Log($"额度查询异常：{ex.Message}");
                await DialogFactory.ShowInfoAsync(this, "刷新额度", $"查询异常：{ex.Message}");
            }
        }

        private static string FormatQuotaDetail(JsonElement root)
        {
            var sb = new StringBuilder();
            sb.AppendLine("Codex 额度使用情况");
            sb.AppendLine();

            if (root.TryGetProperty("wham_usage", out var whamEl) && whamEl.ValueKind == JsonValueKind.Object)
            {
                foreach (string windowKey in new[] { "5h", "7d" })
                {
                    if (whamEl.TryGetProperty(windowKey, out var windowEl) && windowEl.ValueKind == JsonValueKind.Object)
                    {
                        long used = windowEl.TryGetProperty("used", out var u) && u.TryGetInt64(out long uv) ? uv : 0;
                        long limit = windowEl.TryGetProperty("limit", out var l) && l.TryGetInt64(out long lv) ? lv : 0;
                        long remaining = windowEl.TryGetProperty("remaining", out var r) && r.TryGetInt64(out long rv) ? rv : 0;
                        double percent = windowEl.TryGetProperty("percent", out var p) && p.TryGetDouble(out double pv) ? pv : 0;

                        string label = windowKey == "5h" ? "5 小时窗口" : "7 天窗口";
                        sb.AppendLine($"■ {label}");
                        sb.AppendLine($"  已用：{FmtToken(used)} / {FmtToken(limit)} ({percent:F1}%)");
                        sb.AppendLine($"  剩余：{FmtToken(remaining)}");
                        if (windowEl.TryGetProperty("reset_at", out var resetEl))
                            sb.AppendLine($"  重置时间：{resetEl.GetString() ?? ""}");
                        sb.AppendLine();
                    }
                }
            }
            else
            {
                string quotaStatus = root.TryGetProperty("quota_status", out var qsEl) ? qsEl.GetString() ?? "" : "未知";
                sb.AppendLine($"状态：{quotaStatus}");
            }

            return sb.ToString().TrimEnd();
        }

        private static string FmtToken(long n)
        {
            if (n >= 1_000_000) return $"{n / 1_000_000.0:F1}M";
            if (n >= 1_000) return $"{n / 1_000.0:F1}K";
            return n.ToString();
        }
    }
}
