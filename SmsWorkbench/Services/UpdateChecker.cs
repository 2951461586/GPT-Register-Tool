// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

using System.Net;
using System.Net.Http.Headers;
using System.Text.Json.Serialization;

namespace SmsWorkbench
{
    /// <summary>Outcome of one update check. Never carries an exception.</summary>
    /// <remarks>
    /// A failed check is a normal state (offline, GitHub rate limit, a proxy that
    /// only lets the backend through), so the caller gets a result object rather
    /// than a thrown error it has to remember to catch on the UI thread.
    /// </remarks>
    public sealed record UpdateCheckResult
    {
        public bool UpdateAvailable { get; init; }

        public string CurrentVersion { get; init; } = AppVersion.Current;

        public string LatestVersion { get; init; } = "";

        public string ReleaseUrl { get; init; } = "";

        public string PublishedAt { get; init; } = "";

        /// <summary>True when the answer came from the on-disk cache, not the network.</summary>
        public bool FromCache { get; init; }

        /// <summary>Empty on success; a short machine-readable reason otherwise.</summary>
        public string Error { get; init; } = "";

        /// <summary>
        /// When the feed said "not before this instant" (<c>X-RateLimit-Reset</c> /
        /// <c>Retry-After</c>). Null when the failure carries no such hint, which is
        /// the case for a plain network error.
        /// </summary>
        public DateTimeOffset? RetryAfterUtc { get; init; }

        public bool Succeeded => Error.Length == 0;

        /// <summary>
        /// Whole minutes left in the cooldown, rounded up; 0 when there is none or
        /// it has already passed. The UI shows this instead of a bare "later", so a
        /// refused request turns into an instruction.
        /// </summary>
        public int RetryAfterMinutes
        {
            get
            {
                if (RetryAfterUtc is not DateTimeOffset retryAfter)
                    return 0;
                double minutes = (retryAfter - DateTimeOffset.UtcNow).TotalMinutes;
                return minutes <= 0 ? 0 : (int)Math.Ceiling(minutes);
            }
        }
    }

    /// <summary>One feed answer: the document, its validator, or "nothing changed".</summary>
    public sealed record ReleaseFeedResponse
    {
        /// <summary>Latest release document. Empty when <see cref="NotModified"/> is true.</summary>
        public string Json { get; init; } = "";

        /// <summary>Validator for the next conditional request; empty when the host sent none.</summary>
        public string ETag { get; init; } = "";

        /// <summary>True for a <c>304</c>: the cached release is still the latest.</summary>
        public bool NotModified { get; init; }
    }

    /// <summary>
    /// A refused feed request: a machine-readable reason plus, when the host sent
    /// one, the instant before which asking again is pointless.
    /// </summary>
    /// <remarks>
    /// <see cref="Reason"/> values are the same tokens
    /// <see cref="UpdateCheckResult.Error"/> uses, so the checker never has to
    /// re-parse an HTTP status. Deriving from <see cref="HttpRequestException"/>
    /// keeps the existing transport-failure path (and the tests that throw one
    /// directly) working unchanged.
    /// </remarks>
    public sealed class ReleaseFeedException : HttpRequestException
    {
        public ReleaseFeedException(string reason, DateTimeOffset? retryAfterUtc = null)
            : base(reason)
        {
            Reason = reason;
            RetryAfterUtc = retryAfterUtc;
        }

        public string Reason { get; }

        /// <summary>Cooldown the host asked for; null when it did not say.</summary>
        public DateTimeOffset? RetryAfterUtc { get; }
    }

    /// <summary>Asks the release host for the latest published release.</summary>
    /// <remarks>
    /// Split from the checker so the checker's parsing, throttling and
    /// never-throw behaviour are testable without a network or a socket.
    /// </remarks>
    public interface IReleaseFeed
    {
        /// <summary>
        /// Raw JSON of the latest release, or a not-modified answer for
        /// <paramref name="etag"/>. Throws <see cref="ReleaseFeedException"/> (or a
        /// transport exception) on failure.
        /// </summary>
        Task<ReleaseFeedResponse> GetLatestReleaseJsonAsync(string? etag, CancellationToken cancellationToken);
    }

    public interface IUpdateChecker
    {
        /// <summary>
        /// Check for a newer release. Honours the on-disk throttle unless
        /// <paramref name="force"/> is set; never throws for network reasons.
        /// </summary>
        Task<UpdateCheckResult> CheckAsync(CancellationToken cancellationToken, bool force = false);
    }

    /// <summary>
    /// Reads the GitHub Releases API for this repository.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Unauthenticated GitHub API allows 60 requests per hour per address, which
    /// is why the checker throttles: a desktop restarted a few times must not
    /// burn the quota and then report "HTTP 403" as its update verdict.
    /// </para>
    /// <para>
    /// The repository is overridable through <c>GRT_UPDATE_REPO</c> so a fork can
    /// point at its own releases without patching code — and so the check can be
    /// aimed at a local fixture host in tests. <c>GRT_UPDATE_TOKEN</c> is optional
    /// and raises the allowance to 5000 requests per hour.
    /// </para>
    /// </remarks>
    public sealed class GitHubReleaseFeed : IReleaseFeed
    {
        public const string DefaultRepository = "2951461586/GPT-Register-Tool";
        public const string RepositoryEnvironmentVariable = "GRT_UPDATE_REPO";

        /// <summary>Optional token: 5000 requests/hour instead of 60.</summary>
        public const string TokenEnvironmentVariable = "GRT_UPDATE_TOKEN";

        private const string RemainingHeader = "X-RateLimit-Remaining";
        private const string ResetHeader = "X-RateLimit-Reset";

        // The client comes from the desktop egress: one allowlist, one proxy
        // decision and one log line per request, shared with the SMS catalog reads.
        private readonly HttpClient client;
        private readonly string repository;
        private readonly string token;

        public GitHubReleaseFeed(IDesktopEgress egress, string? repository = null)
        {
            client = egress.CreateClient();
            this.repository = ResolveRepository(repository);
            token = (Environment.GetEnvironmentVariable(TokenEnvironmentVariable) ?? "").Trim();
        }

        public string Repository => repository;

        public async Task<ReleaseFeedResponse> GetLatestReleaseJsonAsync(
            string? etag,
            CancellationToken cancellationToken)
        {
            string url = $"https://api.github.com/repos/{repository}/releases/latest";
            using var request = new HttpRequestMessage(HttpMethod.Get, url);
            // GitHub rejects a request without a User-Agent, and the documented
            // Accept header is what pins the response shape this parser expects.
            request.Headers.UserAgent.ParseAdd($"GPT-Register-Tool/{AppVersion.Current}");
            request.Headers.Accept.ParseAdd("application/vnd.github+json");
            // A validator makes the poll free: an unchanged release comes back as
            // 304 and conditional requests do not spend the hourly quota.
            if (!string.IsNullOrWhiteSpace(etag))
                request.Headers.TryAddWithoutValidation("If-None-Match", etag);
            // Optional: an operator with a token gets 5000/h instead of 60/h, which
            // is the honest fix for a shared egress address.
            if (token.Length > 0)
                request.Headers.TryAddWithoutValidation("Authorization", "Bearer " + token);

            using HttpResponseMessage response = await client
                .SendAsync(request, HttpCompletionOption.ResponseContentRead, cancellationToken)
                .ConfigureAwait(false);

            if (response.StatusCode == HttpStatusCode.NotModified)
                return new ReleaseFeedResponse { NotModified = true, ETag = ReadETag(response) };

            if (!response.IsSuccessStatusCode)
                throw ClassifyFailure(response);

            string json = await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
            return new ReleaseFeedResponse { Json = json, ETag = ReadETag(response) };
        }

        /// <summary>
        /// Map one failed response to a reason and a cooldown.
        /// </summary>
        /// <remarks>
        /// <para>
        /// 403 is not one condition. GitHub uses it both for the hourly quota
        /// (<c>X-RateLimit-Remaining: 0</c>) and for a secondary limit or an
        /// address it declines to serve, and only the first is cured by waiting —
        /// so calling a blocked address "too many requests" sends the operator to
        /// wait for nothing. <c>X-RateLimit-Remaining</c> is the discriminator;
        /// absent means unknown, and unknown is reported as
        /// <c>forbidden</c> rather than guessed as a quota.
        /// </para>
        /// <para>
        /// <c>429</c> is the same quota on a newer endpoint, so it is the same
        /// reason token. Public so the mapping is testable without a socket, like
        /// <see cref="GitHubReleaseUpdateChecker.Parse"/>.
        /// </para>
        /// </remarks>
        public static ReleaseFeedException ClassifyFailure(HttpResponseMessage response)
        {
            DateTimeOffset? retryAfter = ReadRetryAfter(response);
            if (response.StatusCode == HttpStatusCode.TooManyRequests)
                return new ReleaseFeedException("rate_limited", retryAfter);
            if (response.StatusCode == HttpStatusCode.Forbidden)
            {
                bool quotaExhausted = ReadRemaining(response) == 0;
                return new ReleaseFeedException(quotaExhausted ? "rate_limited" : "forbidden", retryAfter);
            }

            return new ReleaseFeedException(
                "http_" + ((int)response.StatusCode).ToString(CultureInfo.InvariantCulture),
                retryAfter);
        }

        /// <summary>
        /// <c>Retry-After</c> when the host sent it, otherwise the quota reset
        /// instant. A reset in the past is not a cooldown and yields null.
        /// </summary>
        private static DateTimeOffset? ReadRetryAfter(HttpResponseMessage response)
        {
            RetryConditionHeaderValue? retryAfter = response.Headers.RetryAfter;
            if (retryAfter is not null)
            {
                if (retryAfter.Delta is TimeSpan delta && delta > TimeSpan.Zero)
                    return DateTimeOffset.UtcNow + delta;
                if (retryAfter.Date is DateTimeOffset date)
                    return date;
            }

            foreach (string value in HeaderValues(response, ResetHeader))
            {
                if (long.TryParse(value.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out long unix)
                    && unix > 0)
                {
                    DateTimeOffset reset = DateTimeOffset.FromUnixTimeSeconds(unix);
                    return reset > DateTimeOffset.UtcNow ? reset : null;
                }
            }

            return null;
        }

        private static int? ReadRemaining(HttpResponseMessage response)
        {
            foreach (string value in HeaderValues(response, RemainingHeader))
            {
                if (int.TryParse(value.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out int remaining))
                    return remaining;
            }

            return null;
        }

        private static IEnumerable<string> HeaderValues(HttpResponseMessage response, string name)
            => response.Headers.TryGetValues(name, out IEnumerable<string>? values) ? values : [];

        private static string ReadETag(HttpResponseMessage response)
            => response.Headers.ETag?.ToString() ?? "";

        private static string ResolveRepository(string? explicitRepository)
        {
            foreach (string? candidate in new[] { explicitRepository, Environment.GetEnvironmentVariable(RepositoryEnvironmentVariable) })
            {
                string value = (candidate ?? "").Trim().Trim('/');
                if (value.Length > 0)
                    return value;
            }

            return DefaultRepository;
        }
    }

    /// <summary>
    /// Turns the feed into a verdict, throttled through a small on-disk cache.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The cache exists for three independent reasons: it keeps a burst of restarts
    /// from exhausting the unauthenticated GitHub quota, it keeps the last known
    /// release visible when the network is down, and it remembers a rate-limit
    /// window so restarts stop asking a host that has already said no. A cached
    /// answer is reported as <see cref="UpdateCheckResult.FromCache"/>, so the UI
    /// can tell "up to date as of yesterday" from "up to date right now".
    /// </para>
    /// <para>
    /// Every failure path returns a result. The one exception is
    /// <see cref="OperationCanceledException"/> for the window's lifetime token —
    /// a cancelled check is not a verdict, and swallowing it would leave the
    /// caller believing the check ran. A timeout is <i>not</i> that case: it is a
    /// failure of the network and comes back as <c>timeout</c>.
    /// </para>
    /// </remarks>
    public sealed class GitHubReleaseUpdateChecker : IUpdateChecker
    {
        /// <summary>Six hours: long enough for the quota, short enough for a daily user.</summary>
        public static readonly TimeSpan DefaultThrottle = TimeSpan.FromHours(6);

        /// <summary>
        /// Longest cooldown a single failed response may impose. The quota window
        /// is hourly, so anything beyond this is clock skew or a bogus header and
        /// must not be allowed to pin the check for a day.
        /// </summary>
        public static readonly TimeSpan MaxCooldown = TimeSpan.FromMinutes(65);

        private readonly string cachePath;
        private readonly IReleaseFeed feed;
        private readonly TimeSpan throttle;

        /// <param name="egress">
        /// Required only when no <paramref name="feed"/> is supplied: it builds the
        /// real GitHub feed on the shared, allowlist-guarded client. Tests pass a fake
        /// feed and omit it.
        /// </param>
        public GitHubReleaseUpdateChecker(
            IApplicationPaths paths,
            IReleaseFeed? feed = null,
            TimeSpan? throttle = null,
            IDesktopEgress? egress = null)
        {
            cachePath = Path.Combine(paths.RootDirectory, "runtime", "update_check.json");
            this.feed = feed ?? new GitHubReleaseFeed(
                egress ?? throw new ArgumentNullException(
                    nameof(egress), "an egress is required to build the default release feed"));
            this.throttle = throttle ?? DefaultThrottle;
        }

        public async Task<UpdateCheckResult> CheckAsync(CancellationToken cancellationToken, bool force = false)
        {
            UpdateCheckCache? cached = ReadCache();

            // An active rate-limit window outranks both the throttle and `force`:
            // the host has already said when it will answer again, so a manual
            // click can only spend the first request of the next window on a
            // guaranteed 403. This is what keeps a restart loop quiet.
            if (TryServeCooldown(cached, out UpdateCheckResult coolingDown))
                return coolingDown;

            if (!force && TryServeFromCache(cached, out UpdateCheckResult cachedResult))
                return cachedResult;

            try
            {
                ReleaseFeedResponse response = await feed
                    .GetLatestReleaseJsonAsync(cached?.ETag, cancellationToken)
                    .ConfigureAwait(false);

                if (response.NotModified)
                {
                    // 304: the cached release is still the latest and the request was
                    // free. Re-stamp it so the answer also counts as a fresh check.
                    if (cached is null || cached.LatestVersion.Length == 0 || cached.ReleaseUrl.Length == 0)
                        return new UpdateCheckResult { Error = "not_modified_without_cache" };

                    UpdateCheckCache confirmed = Rechecked(cached, response.ETag);
                    WriteCache(confirmed);
                    return BuildResult(confirmed, fromCache: true);
                }

                UpdateCheckResult fresh = Parse(response.Json);
                if (!fresh.Succeeded)
                    return fresh with { FromCache = false };

                WriteCache(new UpdateCheckCache
                {
                    CheckedAtUnix = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
                    LatestVersion = fresh.LatestVersion,
                    ReleaseUrl = fresh.ReleaseUrl,
                    PublishedAt = fresh.PublishedAt,
                    ETag = Pick(response.ETag, cached?.ETag ?? ""),
                });
                return fresh;
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception ex)
            {
                string reason = Describe(ex);
                DateTimeOffset? retryAfter = CooldownFrom(ex);

                // Persist the window *before* answering, so the next start reads it
                // instead of spending a request to learn the same thing. A plain
                // offline failure carries no window and is deliberately not
                // persisted: retrying it costs nothing and may succeed.
                WriteCooldown(cached, reason, retryAfter);

                // A stale answer beats no answer, but it must say it is stale.
                if (cached is not null && cached.LatestVersion.Length > 0)
                {
                    return BuildResult(cached, fromCache: true)
                        with { Error = reason, RetryAfterUtc = retryAfter };
                }

                return new UpdateCheckResult
                {
                    Error = reason,
                    FromCache = false,
                    RetryAfterUtc = retryAfter,
                };
            }
        }

        /// <summary>Parse one release document; tolerant of missing optional fields.</summary>
        public static UpdateCheckResult Parse(string json)
        {
            try
            {
                using JsonDocument document = JsonDocument.Parse(json);
                JsonElement root = document.RootElement;
                if (root.ValueKind != JsonValueKind.Object)
                    return new UpdateCheckResult { Error = "malformed_release" };

                string tag = AppVersion.Normalize(Text(root, "tag_name"));
                if (tag.Length == 0)
                    return new UpdateCheckResult { Error = "release_without_tag" };

                string url = Text(root, "html_url");
                if (!IsHttpUrl(url))
                    return new UpdateCheckResult { Error = "release_without_url", LatestVersion = tag };

                return new UpdateCheckResult
                {
                    LatestVersion = tag,
                    ReleaseUrl = url,
                    PublishedAt = Text(root, "published_at"),
                    UpdateAvailable = VersionComparer.IsNewer(tag, AppVersion.Current),
                };
            }
            catch (JsonException)
            {
                return new UpdateCheckResult { Error = "malformed_json" };
            }
        }

        /// <summary>
        /// Answer from a stored cooldown without touching the network. False when
        /// there is no window or it has already expired.
        /// </summary>
        private static bool TryServeCooldown(UpdateCheckCache? cached, out UpdateCheckResult result)
        {
            result = new UpdateCheckResult();
            if (cached is null || cached.RetryAfterUnix <= 0)
                return false;

            DateTimeOffset retryAt = DateTimeOffset.FromUnixTimeSeconds(cached.RetryAfterUnix);
            if (retryAt <= DateTimeOffset.UtcNow)
                return false;

            string reason = cached.LastError.Length > 0 ? cached.LastError : "rate_limited";
            result = cached.LatestVersion.Length > 0 && cached.ReleaseUrl.Length > 0
                ? BuildResult(cached, fromCache: true) with { Error = reason, RetryAfterUtc = retryAt }
                : new UpdateCheckResult { Error = reason, FromCache = false, RetryAfterUtc = retryAt };
            return true;
        }

        private bool TryServeFromCache(UpdateCheckCache? cached, out UpdateCheckResult result)
        {
            result = new UpdateCheckResult();
            if (cached is null || cached.LatestVersion.Length == 0 || cached.ReleaseUrl.Length == 0)
                return false;

            long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            long age = now - cached.CheckedAtUnix;
            if (age < 0 || age > (long)throttle.TotalSeconds)
                return false;

            result = BuildResult(cached, fromCache: true);
            return true;
        }

        private static UpdateCheckResult BuildResult(UpdateCheckCache cache, bool fromCache) => new()
        {
            LatestVersion = cache.LatestVersion,
            ReleaseUrl = cache.ReleaseUrl,
            PublishedAt = cache.PublishedAt,
            UpdateAvailable = VersionComparer.IsNewer(cache.LatestVersion, AppVersion.Current),
            FromCache = fromCache,
        };

        private UpdateCheckCache? ReadCache()
        {
            try
            {
                if (!File.Exists(cachePath))
                    return null;
                return JsonSerializer.Deserialize<UpdateCheckCache>(File.ReadAllText(cachePath));
            }
            catch (Exception)
            {
                // A corrupt cache is a cold start, not an error worth surfacing:
                // the next successful check rewrites it.
                return null;
            }
        }

        private void WriteCache(UpdateCheckCache cache)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(cachePath) ?? ".");
                File.WriteAllText(cachePath, JsonSerializer.Serialize(cache));
            }
            catch (Exception)
            {
                // Read-only or locked runtime directory: the check itself still
                // succeeded, so the only cost is a network call next time.
            }
        }

        /// <summary>Record "do not ask again before <paramref name="retryAfter"/>", keeping the last known release.</summary>
        private void WriteCooldown(UpdateCheckCache? cached, string reason, DateTimeOffset? retryAfter)
        {
            if (retryAfter is not DateTimeOffset window)
                return;

            WriteCache(new UpdateCheckCache
            {
                CheckedAtUnix = cached?.CheckedAtUnix ?? 0,
                LatestVersion = cached?.LatestVersion ?? "",
                ReleaseUrl = cached?.ReleaseUrl ?? "",
                PublishedAt = cached?.PublishedAt ?? "",
                ETag = cached?.ETag ?? "",
                RetryAfterUnix = window.ToUnixTimeSeconds(),
                LastError = reason,
            });
        }

        /// <summary>The cache after a 304: same release, fresh timestamp, cleared cooldown.</summary>
        private static UpdateCheckCache Rechecked(UpdateCheckCache cached, string etag) => new()
        {
            CheckedAtUnix = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
            LatestVersion = cached.LatestVersion,
            ReleaseUrl = cached.ReleaseUrl,
            PublishedAt = cached.PublishedAt,
            ETag = Pick(etag, cached.ETag),
        };

        /// <summary>
        /// The cooldown one exception implies, clamped to
        /// <see cref="MaxCooldown"/>. Null for anything that did not come with a
        /// window (plain offline, 5xx, malformed JSON).
        /// </summary>
        private static DateTimeOffset? CooldownFrom(Exception exception)
        {
            if (exception is not ReleaseFeedException refused
                || refused.RetryAfterUtc is not DateTimeOffset retryAfter)
                return null;

            DateTimeOffset now = DateTimeOffset.UtcNow;
            if (retryAfter <= now)
                return null;

            DateTimeOffset ceiling = now + MaxCooldown;
            return retryAfter > ceiling ? ceiling : retryAfter;
        }

        private static string Pick(string preferred, string fallback)
            => preferred.Length > 0 ? preferred : fallback;

        private static string Text(JsonElement element, string name)
            => element.TryGetProperty(name, out JsonElement value) && value.ValueKind == JsonValueKind.String
                ? value.GetString() ?? ""
                : "";

        private static bool IsHttpUrl(string value)
            => Uri.TryCreate(value, UriKind.Absolute, out Uri? uri)
                && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps);

        private static string Describe(Exception exception)
        {
            // A refused request already carries the token the UI switches on.
            if (exception is ReleaseFeedException refused && refused.Reason.Length > 0)
                return refused.Reason;
            if (exception is TaskCanceledException)
                return "timeout";
            if (exception is HttpRequestException http && !string.IsNullOrWhiteSpace(http.Message))
                return http.Message.Length <= 60 ? http.Message : http.Message[..60];
            return exception.GetType().Name;
        }

        /// <summary>On-disk shape of the last answer. Property names stay stable for readers.</summary>
        public sealed class UpdateCheckCache
        {
            [JsonPropertyName("checked_at")]
            public long CheckedAtUnix { get; set; }

            [JsonPropertyName("latest_version")]
            public string LatestVersion { get; set; } = "";

            [JsonPropertyName("release_url")]
            public string ReleaseUrl { get; set; } = "";

            [JsonPropertyName("published_at")]
            public string PublishedAt { get; set; } = "";

            /// <summary>Validator for the conditional request; empty on an older cache file.</summary>
            [JsonPropertyName("etag")]
            public string ETag { get; set; } = "";

            /// <summary>Unix second before which the host refuses requests; 0 when none.</summary>
            [JsonPropertyName("retry_after_unix")]
            public long RetryAfterUnix { get; set; }

            /// <summary>Why the cooldown was recorded, so the served result keeps its reason.</summary>
            [JsonPropertyName("last_error")]
            public string LastError { get; set; } = "";
        }
    }
}
