using System.Text.Json.Serialization;

namespace SmsWorkbench.Services
{
    /// <summary>Outcome of one update check. Never carries an exception.</summary>
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

        public DateTimeOffset? RetryAfterUtc { get; init; }

        public bool Succeeded => Error.Length == 0;

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
        public string Json { get; init; } = "";

        public string ETag { get; init; } = "";

        public bool NotModified { get; init; }
    }

    /// <summary>
    /// A refused feed request: a machine-readable reason plus, when the host sent
    /// one, the instant before which asking again is pointless.
    /// </summary>
    public sealed class ReleaseFeedException : HttpRequestException
    {
        public ReleaseFeedException(string reason, DateTimeOffset? retryAfterUtc = null)
            : base(reason)
        {
            Reason = reason;
            RetryAfterUtc = retryAfterUtc;
        }

        public string Reason { get; }

        public DateTimeOffset? RetryAfterUtc { get; }
    }

    /// <summary>Asks the release host for the latest published release.</summary>
    public interface IReleaseFeed
    {
        Task<ReleaseFeedResponse> GetLatestReleaseJsonAsync(string? etag, CancellationToken cancellationToken);
    }

    public interface IUpdateChecker
    {
        Task<UpdateCheckResult> CheckAsync(CancellationToken cancellationToken, bool force = false);
    }

    /// <summary>
    /// On-disk cache and cooldown store for <see cref="GitHubReleaseUpdateChecker"/>.
    /// </summary>
    internal static class UpdateCheckCacheStore
    {
        public static GitHubReleaseUpdateChecker.UpdateCheckCache? Read(string cachePath)
        {
            try
            {
                if (!File.Exists(cachePath))
                    return null;
                return JsonSerializer.Deserialize<GitHubReleaseUpdateChecker.UpdateCheckCache>(File.ReadAllText(cachePath));
            }
            catch (Exception)
            {
                return null;
            }
        }

        public static void Write(string cachePath, GitHubReleaseUpdateChecker.UpdateCheckCache cache)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(cachePath) ?? ".");
                File.WriteAllText(cachePath, JsonSerializer.Serialize(cache));
            }
            catch (Exception)
            {
            }
        }

        public static void WriteCooldown(
            string cachePath,
            GitHubReleaseUpdateChecker.UpdateCheckCache? cached,
            string reason,
            DateTimeOffset? retryAfter)
        {
            if (retryAfter is not DateTimeOffset window)
                return;

            Write(cachePath, new GitHubReleaseUpdateChecker.UpdateCheckCache
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

        public static bool TryServeCooldown(
            GitHubReleaseUpdateChecker.UpdateCheckCache? cached,
            out UpdateCheckResult result)
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

        public static bool TryServeFromCache(
            GitHubReleaseUpdateChecker.UpdateCheckCache? cached,
            TimeSpan throttle,
            out UpdateCheckResult result)
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

        public static UpdateCheckResult BuildResult(
            GitHubReleaseUpdateChecker.UpdateCheckCache cache,
            bool fromCache) => new()
        {
            LatestVersion = cache.LatestVersion,
            ReleaseUrl = cache.ReleaseUrl,
            PublishedAt = cache.PublishedAt,
            UpdateAvailable = VersionComparer.IsNewer(cache.LatestVersion, AppVersion.Current),
            FromCache = fromCache,
        };

        public static GitHubReleaseUpdateChecker.UpdateCheckCache Rechecked(
            GitHubReleaseUpdateChecker.UpdateCheckCache cached,
            string etag) => new()
        {
            CheckedAtUnix = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
            LatestVersion = cached.LatestVersion,
            ReleaseUrl = cached.ReleaseUrl,
            PublishedAt = cached.PublishedAt,
            ETag = etag.Length > 0 ? etag : cached.ETag,
        };
    }
}
