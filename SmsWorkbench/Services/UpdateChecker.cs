using System.Net;
using System.Net.Http.Headers;
using System.Text.Json.Serialization;

namespace SmsWorkbench.Services
{
    /// <summary>
    /// Reads the GitHub Releases API for this repository.
    /// </summary>
    public sealed class GitHubReleaseFeed : IReleaseFeed
    {
        public const string DefaultRepository = "2951461586/GPT-Register-Tool";
        public const string RepositoryEnvironmentVariable = "GRT_UPDATE_REPO";

        /// <summary>Optional token: 5000 requests/hour instead of 60.</summary>
        public const string TokenEnvironmentVariable = "GRT_UPDATE_TOKEN";

        private const string RemainingHeader = "X-RateLimit-Remaining";
        private const string ResetHeader = "X-RateLimit-Reset";

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
            request.Headers.UserAgent.ParseAdd($"GPT-Register-Tool/{AppVersion.Current}");
            request.Headers.Accept.ParseAdd("application/vnd.github+json");
            if (!string.IsNullOrWhiteSpace(etag))
                request.Headers.TryAddWithoutValidation("If-None-Match", etag);
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
    public sealed class GitHubReleaseUpdateChecker : IUpdateChecker
    {
        public static readonly TimeSpan DefaultThrottle = TimeSpan.FromHours(6);
        public static readonly TimeSpan MaxCooldown = TimeSpan.FromMinutes(65);

        private readonly string cachePath;
        private readonly IReleaseFeed feed;
        private readonly TimeSpan throttle;

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
            UpdateCheckCache? cached = UpdateCheckCacheStore.Read(cachePath);

            if (UpdateCheckCacheStore.TryServeCooldown(cached, out UpdateCheckResult coolingDown))
                return coolingDown;

            if (!force && UpdateCheckCacheStore.TryServeFromCache(cached, throttle, out UpdateCheckResult cachedResult))
                return cachedResult;

            try
            {
                ReleaseFeedResponse response = await feed
                    .GetLatestReleaseJsonAsync(cached?.ETag, cancellationToken)
                    .ConfigureAwait(false);

                if (response.NotModified)
                {
                    if (cached is null || cached.LatestVersion.Length == 0 || cached.ReleaseUrl.Length == 0)
                        return new UpdateCheckResult { Error = "not_modified_without_cache" };

                    UpdateCheckCache confirmed = UpdateCheckCacheStore.Rechecked(cached, response.ETag);
                    UpdateCheckCacheStore.Write(cachePath, confirmed);
                    return UpdateCheckCacheStore.BuildResult(confirmed, fromCache: true);
                }

                UpdateCheckResult fresh = Parse(response.Json);
                if (!fresh.Succeeded)
                    return fresh with { FromCache = false };

                UpdateCheckCacheStore.Write(cachePath, new UpdateCheckCache
                {
                    CheckedAtUnix = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
                    LatestVersion = fresh.LatestVersion,
                    ReleaseUrl = fresh.ReleaseUrl,
                    PublishedAt = fresh.PublishedAt,
                    ETag = response.ETag.Length > 0 ? response.ETag : (cached?.ETag ?? ""),
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

                UpdateCheckCacheStore.WriteCooldown(cachePath, cached, reason, retryAfter);

                if (cached is not null && cached.LatestVersion.Length > 0)
                {
                    return UpdateCheckCacheStore.BuildResult(cached, fromCache: true)
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

        private static string Text(JsonElement element, string name)
            => element.TryGetProperty(name, out JsonElement value) && value.ValueKind == JsonValueKind.String
                ? value.GetString() ?? ""
                : "";

        private static bool IsHttpUrl(string value)
            => Uri.TryCreate(value, UriKind.Absolute, out Uri? uri)
                && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps);

        private static string Describe(Exception exception)
        {
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

            [JsonPropertyName("etag")]
            public string ETag { get; set; } = "";

            [JsonPropertyName("retry_after_unix")]
            public long RetryAfterUnix { get; set; }

            [JsonPropertyName("last_error")]
            public string LastError { get; set; } = "";
        }
    }
}
