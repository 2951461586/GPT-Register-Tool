// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System.Net;
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

        public bool Succeeded => Error.Length == 0;
    }

    /// <summary>Asks the release host for the latest published release.</summary>
    /// <remarks>
    /// Split from the checker so the checker's parsing, throttling and
    /// never-throw behaviour are testable without a network or a socket.
    /// </remarks>
    public interface IReleaseFeed
    {
        /// <summary>Raw JSON of the latest release. Throws on transport/protocol failure.</summary>
        Task<string> GetLatestReleaseJsonAsync(CancellationToken cancellationToken);
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
    /// aimed at a local fixture host in tests.
    /// </para>
    /// </remarks>
    public sealed class GitHubReleaseFeed : IReleaseFeed
    {
        public const string DefaultRepository = "2951461586/GPT-Register-Tool";
        public const string RepositoryEnvironmentVariable = "GRT_UPDATE_REPO";

        /// <summary>One client for the process: the check runs at most a few times a day.</summary>
        private static readonly HttpClient Client = CreateClient();

        private readonly string repository;

        public GitHubReleaseFeed(string? repository = null)
        {
            this.repository = ResolveRepository(repository);
        }

        public string Repository => repository;

        public async Task<string> GetLatestReleaseJsonAsync(CancellationToken cancellationToken)
        {
            string url = $"https://api.github.com/repos/{repository}/releases/latest";
            using var request = new HttpRequestMessage(HttpMethod.Get, url);
            // GitHub rejects a request without a User-Agent, and the documented
            // Accept header is what pins the response shape this parser expects.
            request.Headers.UserAgent.ParseAdd($"GPT-Register-Tool/{AppVersion.Current}");
            request.Headers.Accept.ParseAdd("application/vnd.github+json");

            using HttpResponseMessage response = await Client
                .SendAsync(request, HttpCompletionOption.ResponseContentRead, cancellationToken)
                .ConfigureAwait(false);

            if (!response.IsSuccessStatusCode)
            {
                string reason = response.StatusCode == HttpStatusCode.Forbidden
                    ? "rate_limited"
                    : "http_" + (int)response.StatusCode;
                throw new HttpRequestException(reason);
            }

            return await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
        }

        private static HttpClient CreateClient()
            => new() { Timeout = TimeSpan.FromSeconds(10) };

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
    /// The cache exists for two independent reasons: it keeps a burst of restarts
    /// from exhausting the unauthenticated GitHub quota, and it keeps the last
    /// known release visible when the network is down. A cached answer is reported
    /// as <see cref="UpdateCheckResult.FromCache"/>, so the UI can tell "up to
    /// date as of yesterday" from "up to date right now".
    /// </para>
    /// <para>
    /// Every failure path returns a result. The one exception is
    /// <see cref="OperationCanceledException"/> for the window's lifetime token —
    /// a cancelled check is not a verdict, and swallowing it would leave the
    /// caller believing the check ran.
    /// </para>
    /// </remarks>
    public sealed class GitHubReleaseUpdateChecker : IUpdateChecker
    {
        /// <summary>Six hours: long enough for the quota, short enough for a daily user.</summary>
        public static readonly TimeSpan DefaultThrottle = TimeSpan.FromHours(6);

        private readonly string cachePath;
        private readonly IReleaseFeed feed;
        private readonly TimeSpan throttle;

        public GitHubReleaseUpdateChecker(IApplicationPaths paths, IReleaseFeed? feed = null, TimeSpan? throttle = null)
        {
            cachePath = Path.Combine(paths.RootDirectory, "runtime", "update_check.json");
            this.feed = feed ?? new GitHubReleaseFeed();
            this.throttle = throttle ?? DefaultThrottle;
        }

        public async Task<UpdateCheckResult> CheckAsync(CancellationToken cancellationToken, bool force = false)
        {
            UpdateCheckCache? cached = ReadCache();
            if (!force && TryServeFromCache(cached, out UpdateCheckResult cachedResult))
                return cachedResult;

            try
            {
                string json = await feed.GetLatestReleaseJsonAsync(cancellationToken).ConfigureAwait(false);
                UpdateCheckResult fresh = Parse(json);
                if (!fresh.Succeeded)
                    return fresh with { FromCache = false };
                WriteCache(new UpdateCheckCache
                {
                    CheckedAtUnix = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
                    LatestVersion = fresh.LatestVersion,
                    ReleaseUrl = fresh.ReleaseUrl,
                    PublishedAt = fresh.PublishedAt,
                });
                return fresh;
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch (Exception ex)
            {
                string reason = Describe(ex);
                // A stale answer beats no answer, but it must say it is stale.
                if (cached is not null && cached.LatestVersion.Length > 0)
                {
                    return BuildResult(cached, fromCache: true) with { Error = reason };
                }

                return new UpdateCheckResult { Error = reason, FromCache = false };
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

        private static string Text(JsonElement element, string name)
            => element.TryGetProperty(name, out JsonElement value) && value.ValueKind == JsonValueKind.String
                ? value.GetString() ?? ""
                : "";

        private static bool IsHttpUrl(string value)
            => Uri.TryCreate(value, UriKind.Absolute, out Uri? uri)
                && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps);

        private static string Describe(Exception exception)
        {
            if (exception is HttpRequestException http && !string.IsNullOrWhiteSpace(http.Message))
                return http.Message.Length <= 60 ? http.Message : http.Message[..60];
            if (exception is TaskCanceledException)
                return "timeout";
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
        }
    }
}
