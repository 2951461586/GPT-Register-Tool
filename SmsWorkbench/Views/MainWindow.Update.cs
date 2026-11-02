// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// Version label in the title bar plus the one automatic update check.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The check is advisory by design: it never blocks startup, never installs
    /// anything, and a failure is silent unless the operator asked for it by
    /// clicking the version. That shape follows from what it can actually do —
    /// the desktop ships as a zip/exe next to a Python core, so "update" is a
    /// download page, not an in-place patch. The header only has to answer
    /// "am I behind?" and offer the link.
    /// </para>
    /// <para>
    /// Off the UI thread until the verdict arrives: the request runs inside
    /// <see cref="RunUiTask"/>, and <see cref="IUpdateChecker"/> already contains
    /// every failure (offline, rate limit, malformed JSON) so nothing here has to
    /// distinguish "no network" from "GitHub said no".
    /// </para>
    /// </remarks>
    public partial class MainWindow
    {
        private readonly IUpdateChecker updateChecker;
        private string updateText = "";
        private string updateTooltip = "";
        private string updateReleaseUrl = "";
        private string versionTooltipOverride = "";
        private bool updateAvailable;
        private bool updateCheckRunning;

        /// <summary>Bare version for the header, e.g. <c>v2026.10.28</c> (or <c>dev</c>).</summary>
        public string VersionText => AppVersion.Display;

        /// <summary>
        /// Tooltip on the version label; the label doubles as the manual check
        /// button. While a rate-limit window is open this carries the retry
        /// instant, because the label is the one surface that is always visible —
        /// the update chip hides itself while no newer release is known.
        /// </summary>
        public string VersionTooltip => versionTooltipOverride.Length > 0
            ? versionTooltipOverride
            : Loc.F("当前版本 {0} · 点击检查更新", AppVersion.Display);

        /// <summary>Chip text; empty while no newer release is known.</summary>
        public string UpdateText
        {
            get => updateText;
            private set
            {
                if (updateText == value) return;
                updateText = value;
                OnPropertyChanged(nameof(UpdateText));
            }
        }

        /// <summary>Tooltip on the chip: latest version and its publication date.</summary>
        public string UpdateTooltip
        {
            get => updateTooltip;
            private set
            {
                if (updateTooltip == value) return;
                updateTooltip = value;
                OnPropertyChanged(nameof(UpdateTooltip));
            }
        }

        /// <summary>True when a newer release exists; drives the chip's visibility.</summary>
        public bool UpdateAvailable
        {
            get => updateAvailable;
            private set
            {
                if (updateAvailable == value) return;
                updateAvailable = value;
                OnPropertyChanged(nameof(UpdateAvailable));
            }
        }

        /// <summary>Manual check (the version label is the button).</summary>
        public RelayCommand CheckUpdatesCommand { get; private set; } = null!;

        /// <summary>Opens the release page in the default browser.</summary>
        public RelayCommand OpenReleaseCommand { get; private set; } = null!;

        /// <summary>
        /// Wire the commands and schedule the start-up check. Called from the
        /// constructor before <c>DataContext = this</c>, so the bindings resolve
        /// their first value without a notification round-trip.
        /// </summary>
        private void InitializeUpdateCheck()
        {
            CheckUpdatesCommand = new RelayCommand(() => CheckForUpdates(force: true));
            OpenReleaseCommand = new RelayCommand(OpenReleasePage);
            Loaded += OnLoadedCheckForUpdates;
        }

        private void OnLoadedCheckForUpdates(object sender, RoutedEventArgs e)
        {
            // One shot: the window can be hidden/shown, and a throttled check on
            // every show would spend the GitHub quota for no new information.
            Loaded -= OnLoadedCheckForUpdates;
            CheckForUpdates(force: false);
        }

        private void CheckForUpdates(bool force)
        {
            // A second click while the first request is in flight must not fork a
            // parallel check; the throttle would collapse them anyway, but the
            // announcements would double.
            if (updateCheckRunning) return;
            updateCheckRunning = true;
            RunUiTask(async () =>
            {
                try
                {
                    UpdateCheckResult result = await updateChecker
                        .CheckAsync(_lifetimeCts.Token, force)
                        .ConfigureAwait(true);
                    ApplyUpdateResult(result, announce: force);
                }
                finally
                {
                    updateCheckRunning = false;
                }
            });
        }

        /// <summary>
        /// Fold one verdict into the header. <paramref name="announce"/> is true
        /// only for a manual check: the automatic one must not pop a snackbar on
        /// every start, and a silent failure is the correct default there.
        /// </summary>
        private void ApplyUpdateResult(UpdateCheckResult result, bool announce)
        {
            if (!result.Succeeded)
            {
                int retryMinutes = result.RetryAfterMinutes;
                if (announce)
                {
                    NotifyWarning(Loc.F("检查更新失败：{0}", Loc.T(result.Error switch
                    {
                        // A quota can be waited out, and naming the wait turns a
                        // dead end into an instruction.
                        "rate_limited" => retryMinutes > 0
                            ? Loc.F("请求过于频繁，约 {0} 分钟后可重试", retryMinutes)
                            : "请求过于频繁，请稍后再试",
                        // 403 with quota left is not a rate limit; advice to wait
                        // would be wrong and the cure is the network egress.
                        "forbidden" => "请求被 GitHub 拒绝，可能是当前网络出口被限制",
                        "timeout" => "网络超时",
                        _ => "网络不可用",
                    })));
                }

                SetVersionTooltip(retryMinutes > 0
                    ? Loc.F("当前版本 {0} · 更新检查受限，约 {1} 分钟后可重试", AppVersion.Display, retryMinutes)
                    : "");
                if (retryMinutes > 0 && UpdateAvailable)
                    UpdateTooltip += Loc.F("（检查受限，约 {0} 分钟后可重试）", retryMinutes);

                Log("更新检查失败：" + result.Error
                    + (retryMinutes > 0 ? $"（约 {retryMinutes} 分钟后可重试）" : ""));
                return;
            }

            // A verdict replaces the cooldown hint, including one recorded by an
            // earlier run that this check has now outgrown.
            SetVersionTooltip("");
            updateReleaseUrl = result.ReleaseUrl;
            UpdateAvailable = result.UpdateAvailable;
            // Three relations, not two. "No update" covers both "you are on the
            // newest release" and "this build is ahead of the newest release", and
            // the second one is not the same statement: a checkout stamped today,
            // or a fork with its own numbering, is ahead simply because the release
            // does not exist yet. Reporting that as 已是最新 hides it — the
            // operator then wonders why the public release is older than their build.
            int relation = VersionComparer.Compare(result.LatestVersion, AppVersion.Current);
            string latestTag = "v" + result.LatestVersion;
            if (relation > 0)
            {
                UpdateText = Loc.F("发现新版本 {0}", latestTag);
                UpdateTooltip = Loc.F(
                    "最新版本 {0} · 发布于 {1} · 点击打开下载页",
                    latestTag,
                    FormatReleaseDate(result.PublishedAt));
            }
            else
            {
                UpdateText = "";
                UpdateTooltip = relation == 0
                    ? Loc.T("已是最新版本")
                    : Loc.F("本地版本 {0} 高于上游最近发布 {1}", AppVersion.Display, latestTag);
            }

            Log(
                $"更新检查：当前 {AppVersion.Display}，上游 {latestTag}" +
                (result.FromCache ? "（缓存）" : "") +
                (relation > 0 ? "，有可用更新" : relation == 0 ? "，与上游一致" : "，本地版本高于上游最近发布"));

            if (!announce)
                return;

            if (relation > 0)
            {
                NotifyInfo(Loc.F("发现新版本 {0}，点击顶部版本号旁的提示打开下载页", latestTag));
            }
            else if (relation == 0)
            {
                NotifySuccess(Loc.T("已是最新版本"));
            }
            else
            {
                NotifyInfo(Loc.F("当前构建 {0} 高于上游最近发布 {1}（尚未发布）", AppVersion.Display, latestTag));
            }
        }

        private void SetVersionTooltip(string value)
        {
            if (versionTooltipOverride == value) return;
            versionTooltipOverride = value;
            OnPropertyChanged(nameof(VersionTooltip));
        }

        private void OpenReleasePage()
        {
            if (updateReleaseUrl.Length == 0) return;
            OpenUrl(updateReleaseUrl);
        }

        /// <summary>
        /// <c>published_at</c> is ISO-8601 from the API; render the local date and
        /// echo the raw string when it does not parse, rather than showing the
        /// operator a blank where evidence should be.
        /// </summary>
        private static string FormatReleaseDate(string raw)
            => DateTimeOffset.TryParse(raw, CultureInfo.InvariantCulture, DateTimeStyles.None, out DateTimeOffset parsed)
                ? parsed.ToLocalTime().ToString("yyyy-MM-dd", CultureInfo.InvariantCulture)
                : raw;
    }
}
