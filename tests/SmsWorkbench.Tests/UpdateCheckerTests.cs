using System.Globalization;
using System.Net;
using System.Net.Http.Headers;
using System.Text.Json;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

/// <summary>
/// The update check is advisory, so every path must end in a result: a thrown
/// error on start-up would be the one place a network problem could break the
/// window. These tests pin the things that can silently go wrong —
/// calendar-version comparison, tolerating a release document, the throttle that
/// keeps the unauthenticated GitHub quota alive across restarts, and the
/// rate-limit window that must stop a restart loop from spending the next
/// window's first request on a guaranteed 403.
/// </summary>
public sealed class UpdateCheckerTests
{
    private const string Etag = "\"release-1\"";

    private const string NewerReleaseJson = """
        {
          "tag_name": "v2099.01.01",
          "html_url": "https://github.com/2951461586/GPT-Register-Tool/releases/tag/v2099.01.01",
          "published_at": "2099-01-01T10:00:00Z",
          "name": "v2099.01.01"
        }
        """;

    [Theory]
    [InlineData("v2026.10.28", "2026.10.28")]
    [InlineData("2026.10.28+build.7", "2026.10.28")]
    [InlineData("v2026.10.28-rc1", "2026.10.28")]
    [InlineData("2026.10.28.0", "2026.10.28")]
    [InlineData("2026.10", "2026.10")]
    [InlineData("", "")]
    public void Normalize_reduces_every_release_spelling_to_its_core(string raw, string expected)
        => Assert.Equal(expected, AppVersion.Normalize(raw));

    [Theory]
    [InlineData("2026.10.29", "2026.10.28", true)]
    [InlineData("2026.10.27", "2026.10.28", false)]
    [InlineData("2026.10.28", "2026.10.28", false)]
    [InlineData("v2026.10.28+build", "2026.10.28", false)]
    [InlineData("2026.11", "2026.10.28", true)]
    [InlineData("2026.10", "2026.10.0", false)]
    [InlineData("2027.1.1", "2026.12.31", true)]
    public void IsNewer_compares_segments_numerically(string candidate, string current, bool expected)
        => Assert.Equal(expected, VersionComparer.IsNewer(candidate, current));

    [Fact]
    public void Parse_reads_tag_url_and_date()
    {
        UpdateCheckResult result = GitHubReleaseUpdateChecker.Parse(NewerReleaseJson);

        Assert.True(result.Succeeded, result.Error);
        Assert.Equal("2099.01.01", result.LatestVersion);
        Assert.Equal("https://github.com/2951461586/GPT-Register-Tool/releases/tag/v2099.01.01", result.ReleaseUrl);
        Assert.Equal("2099-01-01T10:00:00Z", result.PublishedAt);
        // Compared against whatever this build stamps, so the assertion stays
        // true for a dev build (0.0.0) and for a stamped release.
        Assert.Equal(VersionComparer.IsNewer("2099.01.01", AppVersion.Current), result.UpdateAvailable);
    }

    [Theory]
    [InlineData("""{"html_url":"https://example.test/r"}""", "release_without_tag")]
    [InlineData("""{"tag_name":"v1.2.3"}""", "release_without_url")]
    [InlineData("""{"tag_name":"v1.2.3","html_url":"javascript:alert(1)"}""", "release_without_url")]
    [InlineData("not json", "malformed_json")]
    [InlineData("[]", "malformed_release")]
    public void Parse_rejects_incomplete_or_hostile_documents(string json, string expectedError)
    {
        UpdateCheckResult result = GitHubReleaseUpdateChecker.Parse(json);

        Assert.False(result.Succeeded);
        Assert.Equal(expectedError, result.Error);
    }

    [Fact]
    public async Task First_check_hits_the_feed_and_the_second_serves_the_cache()
    {
        using var directory = new TempDirectory();
        var feed = new FakeReleaseFeed { Json = NewerReleaseJson };
        var checker = new GitHubReleaseUpdateChecker(
            new TestApplicationPaths(directory.Path), feed, TimeSpan.FromHours(6));

        UpdateCheckResult first = await checker.CheckAsync(CancellationToken.None);
        UpdateCheckResult second = await checker.CheckAsync(CancellationToken.None);

        Assert.Equal(1, feed.Calls);
        Assert.False(first.FromCache);
        Assert.True(second.FromCache);
        Assert.Equal(first.LatestVersion, second.LatestVersion);
    }

    [Fact]
    public async Task Force_bypasses_the_throttle()
    {
        using var directory = new TempDirectory();
        var feed = new FakeReleaseFeed { Json = NewerReleaseJson };
        var checker = new GitHubReleaseUpdateChecker(
            new TestApplicationPaths(directory.Path), feed, TimeSpan.FromHours(6));

        await checker.CheckAsync(CancellationToken.None);
        UpdateCheckResult forced = await checker.CheckAsync(CancellationToken.None, force: true);

        Assert.Equal(2, feed.Calls);
        Assert.False(forced.FromCache);
    }

    [Fact]
    public async Task Transport_failure_is_a_result_not_an_exception()
    {
        using var directory = new TempDirectory();
        var feed = new FakeReleaseFeed { Failure = new HttpRequestException("rate_limited") };
        var checker = new GitHubReleaseUpdateChecker(new TestApplicationPaths(directory.Path), feed);

        UpdateCheckResult result = await checker.CheckAsync(CancellationToken.None);

        Assert.False(result.Succeeded);
        Assert.Equal("rate_limited", result.Error);
        Assert.False(result.UpdateAvailable);
    }

    [Fact]
    public async Task A_failure_after_a_good_check_still_reports_the_cached_release()
    {
        using var directory = new TempDirectory();
        var feed = new FakeReleaseFeed { Json = NewerReleaseJson };
        var checker = new GitHubReleaseUpdateChecker(
            new TestApplicationPaths(directory.Path), feed, TimeSpan.FromHours(6));
        await checker.CheckAsync(CancellationToken.None);

        feed.Failure = new HttpRequestException("HTTP 502");
        UpdateCheckResult offline = await checker.CheckAsync(CancellationToken.None, force: true);

        // Stale data, labelled stale: the version is still readable, and the
        // error says why it may be out of date.
        Assert.Equal("2099.01.01", offline.LatestVersion);
        Assert.True(offline.FromCache);
        Assert.Contains("502", offline.Error, StringComparison.Ordinal);
    }

    // ── Rate-limit window ────────────────────────────────────────────────

    [Fact]
    public async Task A_rate_limited_check_is_remembered_so_a_restart_does_not_ask_again()
    {
        using var directory = new TempDirectory();
        var feed = new FakeReleaseFeed
        {
            Failure = new ReleaseFeedException("rate_limited", DateTimeOffset.UtcNow.AddMinutes(30)),
        };
        var checker = new GitHubReleaseUpdateChecker(new TestApplicationPaths(directory.Path), feed);

        UpdateCheckResult refused = await checker.CheckAsync(CancellationToken.None);
        Assert.Equal("rate_limited", refused.Error);
        Assert.InRange(refused.RetryAfterMinutes, 28, 30);

        // A restart (new process, same runtime directory) must read the window off
        // disk instead of spending a request to learn the same refusal.
        var restarted = new GitHubReleaseUpdateChecker(new TestApplicationPaths(directory.Path), feed);
        UpdateCheckResult cooling = await restarted.CheckAsync(CancellationToken.None);

        Assert.Equal(1, feed.Calls);
        Assert.Equal("rate_limited", cooling.Error);
        Assert.InRange(cooling.RetryAfterMinutes, 28, 30);
    }

    [Fact]
    public async Task Force_cannot_override_a_rate_limit_window()
    {
        using var directory = new TempDirectory();
        var feed = new FakeReleaseFeed
        {
            Failure = new ReleaseFeedException("rate_limited", DateTimeOffset.UtcNow.AddMinutes(20)),
        };
        var checker = new GitHubReleaseUpdateChecker(new TestApplicationPaths(directory.Path), feed);

        await checker.CheckAsync(CancellationToken.None);
        // Clicking the version is a request to refresh, not a request to be lied
        // to: the host has said when it will answer, so the click is a no-op.
        UpdateCheckResult forced = await checker.CheckAsync(CancellationToken.None, force: true);

        Assert.Equal(1, feed.Calls);
        Assert.Equal("rate_limited", forced.Error);
        Assert.True(forced.RetryAfterMinutes > 0);
    }

    [Fact]
    public async Task A_rate_limited_check_still_reports_the_cached_release_as_stale()
    {
        using var directory = new TempDirectory();
        var feed = new FakeReleaseFeed { Json = NewerReleaseJson, ETag = Etag };
        var checker = new GitHubReleaseUpdateChecker(new TestApplicationPaths(directory.Path), feed);
        await checker.CheckAsync(CancellationToken.None);

        feed.Failure = new ReleaseFeedException("rate_limited", DateTimeOffset.UtcNow.AddMinutes(45));
        UpdateCheckResult refused = await checker.CheckAsync(CancellationToken.None, force: true);

        Assert.Equal("2099.01.01", refused.LatestVersion);
        Assert.True(refused.FromCache);
        Assert.Equal("rate_limited", refused.Error);
        Assert.InRange(refused.RetryAfterMinutes, 43, 45);

        // ...and the cached pair is what the next start sees, with the reason kept.
        var restarted = new GitHubReleaseUpdateChecker(new TestApplicationPaths(directory.Path), feed);
        UpdateCheckResult cooling = await restarted.CheckAsync(CancellationToken.None);

        Assert.Equal(2, feed.Calls);
        Assert.True(cooling.FromCache);
        Assert.Equal("rate_limited", cooling.Error);
        Assert.Equal("2099.01.01", cooling.LatestVersion);
    }

    [Fact]
    public async Task An_expired_window_is_not_served_and_the_check_goes_out_again()
    {
        using var directory = new TempDirectory();
        Directory.CreateDirectory(Path.Combine(directory.Path, "runtime"));
        long expired = DateTimeOffset.UtcNow.AddMinutes(-5).ToUnixTimeSeconds();
        File.WriteAllText(
            Path.Combine(directory.Path, "runtime", "update_check.json"),
            $$"""
            {"checked_at":0,"latest_version":"2026.10.28","release_url":"https://example.test/r","retry_after_unix":{{expired}},"last_error":"rate_limited"}
            """);

        var feed = new FakeReleaseFeed { Json = NewerReleaseJson };
        var checker = new GitHubReleaseUpdateChecker(new TestApplicationPaths(directory.Path), feed);

        UpdateCheckResult result = await checker.CheckAsync(CancellationToken.None);

        Assert.Equal(1, feed.Calls);
        Assert.True(result.Succeeded, result.Error);
        Assert.Equal(0, result.RetryAfterMinutes);
    }

    [Fact]
    public async Task A_plain_offline_failure_is_not_turned_into_a_cooldown()
    {
        using var directory = new TempDirectory();
        var feed = new FakeReleaseFeed { Failure = new HttpRequestException("connection refused") };
        var checker = new GitHubReleaseUpdateChecker(new TestApplicationPaths(directory.Path), feed);

        UpdateCheckResult offline = await checker.CheckAsync(CancellationToken.None);
        Assert.False(offline.Succeeded);
        Assert.Null(offline.RetryAfterUtc);

        // Retrying costs nothing and may succeed, so nothing is written down.
        var restarted = new GitHubReleaseUpdateChecker(new TestApplicationPaths(directory.Path), feed);
        await restarted.CheckAsync(CancellationToken.None);

        Assert.Equal(2, feed.Calls);
    }

    [Fact]
    public async Task A_far_future_reset_is_clamped_to_one_quota_window()
    {
        using var directory = new TempDirectory();
        var feed = new FakeReleaseFeed
        {
            // Clock skew or a bogus header must not pin the check for a day.
            Failure = new ReleaseFeedException("rate_limited", DateTimeOffset.UtcNow.AddDays(1)),
        };
        var checker = new GitHubReleaseUpdateChecker(new TestApplicationPaths(directory.Path), feed);

        UpdateCheckResult refused = await checker.CheckAsync(CancellationToken.None);

        Assert.InRange(
            refused.RetryAfterMinutes,
            (int)GitHubReleaseUpdateChecker.MaxCooldown.TotalMinutes - 1,
            (int)GitHubReleaseUpdateChecker.MaxCooldown.TotalMinutes);
    }

    // ── Conditional request (304) ────────────────────────────────────────

    [Fact]
    public async Task A_successful_check_stores_the_validator_for_the_next_request()
    {
        using var directory = new TempDirectory();
        var feed = new FakeReleaseFeed { Json = NewerReleaseJson, ETag = Etag };
        var checker = new GitHubReleaseUpdateChecker(new TestApplicationPaths(directory.Path), feed);

        await checker.CheckAsync(CancellationToken.None);
        GitHubReleaseUpdateChecker.UpdateCheckCache? cache = ReadCache(directory.Path);

        Assert.NotNull(cache);
        Assert.Equal(Etag, cache.ETag);
        Assert.Equal(0, cache.RetryAfterUnix);
        Assert.Equal("", cache.LastError);
    }

    [Fact]
    public async Task A_304_confirms_the_cache_without_spending_a_second_request()
    {
        using var directory = new TempDirectory();
        var feed = new FakeReleaseFeed { Json = NewerReleaseJson, ETag = Etag };
        var checker = new GitHubReleaseUpdateChecker(new TestApplicationPaths(directory.Path), feed);
        await checker.CheckAsync(CancellationToken.None);
        long before = ReadCache(directory.Path)!.CheckedAtUnix;

        // The release has not changed: GitHub answers 304 instead of the document,
        // and that answer does not count against the hourly quota.
        feed.NotModified = true;
        UpdateCheckResult confirmed = await checker.CheckAsync(CancellationToken.None, force: true);

        Assert.Equal(2, feed.Calls);
        Assert.Equal(Etag, feed.LastEtag);
        Assert.True(confirmed.FromCache);
        Assert.Equal("2099.01.01", confirmed.LatestVersion);

        // Re-stamped, so the six-hour throttle now starts from this confirmation.
        GitHubReleaseUpdateChecker.UpdateCheckCache? cache = ReadCache(directory.Path);
        Assert.NotNull(cache);
        Assert.True(cache.CheckedAtUnix >= before);
        Assert.Equal(Etag, cache.ETag);
    }

    [Fact]
    public async Task A_304_without_a_usable_cache_is_an_error_not_a_blank_verdict()
    {
        using var directory = new TempDirectory();
        var feed = new FakeReleaseFeed { NotModified = true, ETag = Etag };
        var checker = new GitHubReleaseUpdateChecker(new TestApplicationPaths(directory.Path), feed);

        UpdateCheckResult result = await checker.CheckAsync(CancellationToken.None);

        Assert.Equal("not_modified_without_cache", result.Error);
    }

    [Fact]
    public void Display_marks_an_unstamped_build_as_dev()
    {
        // The published artefact is stamped by build_dotnet.ps1; a plain `dotnet
        // build` leaves 0.0.0, and claiming "v0.0.0" would read as a release.
        Assert.Equal(
            AppVersion.Current == AppVersion.Unknown ? "dev" : "v" + AppVersion.Current,
            AppVersion.Display);
    }

    private static GitHubReleaseUpdateChecker.UpdateCheckCache? ReadCache(string root)
    {
        string path = Path.Combine(root, "runtime", "update_check.json");
        return File.Exists(path)
            ? JsonSerializer.Deserialize<GitHubReleaseUpdateChecker.UpdateCheckCache>(File.ReadAllText(path))
            : null;
    }

    private sealed class FakeReleaseFeed : IReleaseFeed
    {
        public string Json { get; set; } = "";

        public string ETag { get; set; } = "";

        public bool NotModified { get; set; }

        public Exception? Failure { get; set; }

        public int Calls { get; private set; }

        public string? LastEtag { get; private set; }

        public Task<ReleaseFeedResponse> GetLatestReleaseJsonAsync(
            string? etag,
            CancellationToken cancellationToken)
        {
            Calls++;
            LastEtag = etag;
            cancellationToken.ThrowIfCancellationRequested();
            if (Failure is not null)
                return Task.FromException<ReleaseFeedResponse>(Failure);

            return Task.FromResult(NotModified
                ? new ReleaseFeedResponse { NotModified = true, ETag = ETag }
                : new ReleaseFeedResponse { Json = Json, ETag = ETag });
        }
    }

    private sealed class TempDirectory : IDisposable
    {
        public TempDirectory()
        {
            Path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "grt-update-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(Path);
        }

        public string Path { get; }

        public void Dispose()
        {
            try
            {
                Directory.Delete(Path, recursive: true);
            }
            catch (IOException)
            {
                // A locked temp directory is not this test's failure to report.
            }
        }
    }
}

/// <summary>
/// The HTTP half of the feed: which request goes out and how a refusal is read.
/// </summary>
/// <remarks>
/// No socket is opened. A stub handler returns the response, which is the only
/// way to pin the two things a real GitHub call cannot be trusted to reproduce on
/// demand: a 403 with quota left over (secondary limit / blocked address) and the
/// <c>X-RateLimit-Reset</c> that becomes the cooldown.
/// </remarks>
public sealed class ReleaseFeedTests
{
    [Fact]
    public void A_403_with_no_quota_left_is_rate_limited_and_carries_the_reset()
    {
        long reset = DateTimeOffset.UtcNow.AddMinutes(20).ToUnixTimeSeconds();
        using HttpResponseMessage response = Refusal(
            HttpStatusCode.Forbidden,
            ("X-RateLimit-Remaining", "0"),
            ("X-RateLimit-Reset", reset.ToString(CultureInfo.InvariantCulture)));

        ReleaseFeedException refusal = GitHubReleaseFeed.ClassifyFailure(response);

        Assert.Equal("rate_limited", refusal.Reason);
        Assert.NotNull(refusal.RetryAfterUtc);
        Assert.InRange(
            (refusal.RetryAfterUtc!.Value - DateTimeOffset.UtcNow).TotalMinutes,
            18,
            20);
    }

    [Fact]
    public void A_403_with_quota_left_is_forbidden_not_rate_limited()
    {
        using HttpResponseMessage response = Refusal(
            HttpStatusCode.Forbidden,
            ("X-RateLimit-Remaining", "45"),
            ("X-RateLimit-Reset", DateTimeOffset.UtcNow.AddMinutes(20).ToUnixTimeSeconds().ToString(CultureInfo.InvariantCulture)));

        ReleaseFeedException refusal = GitHubReleaseFeed.ClassifyFailure(response);

        // An address GitHub declines to serve is not cured by waiting, so the UI
        // must not tell the operator to wait.
        Assert.Equal("forbidden", refusal.Reason);
    }

    [Fact]
    public void A_403_without_quota_headers_is_forbidden()
    {
        using HttpResponseMessage response = Refusal(HttpStatusCode.Forbidden);

        // Unknown is reported as unknown, not guessed as an exhausted quota.
        Assert.Equal("forbidden", GitHubReleaseFeed.ClassifyFailure(response).Reason);
        Assert.Null(GitHubReleaseFeed.ClassifyFailure(response).RetryAfterUtc);
    }

    [Fact]
    public void A_429_is_rate_limited_and_honours_retry_after_seconds()
    {
        using HttpResponseMessage response = Refusal(HttpStatusCode.TooManyRequests, ("Retry-After", "30"));

        ReleaseFeedException refusal = GitHubReleaseFeed.ClassifyFailure(response);

        Assert.Equal("rate_limited", refusal.Reason);
        Assert.NotNull(refusal.RetryAfterUtc);
        Assert.InRange(
            (refusal.RetryAfterUtc!.Value - DateTimeOffset.UtcNow).TotalSeconds,
            20,
            30);
    }

    [Fact]
    public void Another_status_keeps_its_code()
    {
        using HttpResponseMessage response = Refusal(HttpStatusCode.BadGateway);

        Assert.Equal("http_502", GitHubReleaseFeed.ClassifyFailure(response).Reason);
    }

    [Fact]
    public async Task The_feed_sends_the_validator_and_the_optional_token()
    {
        string? previous = Environment.GetEnvironmentVariable(GitHubReleaseFeed.TokenEnvironmentVariable);
        try
        {
            Environment.SetEnvironmentVariable(GitHubReleaseFeed.TokenEnvironmentVariable, "fixture-token");
            var handler = new CapturingHandler(_ => Json("""{"tag_name":"v1.0.0"}""", "\"v2\""));
            using var egress = new StubEgress(handler);
            var feed = new GitHubReleaseFeed(egress);

            ReleaseFeedResponse response = await feed.GetLatestReleaseJsonAsync("W/\"v1\"", CancellationToken.None);

            Assert.Equal("""{"tag_name":"v1.0.0"}""", response.Json);
            Assert.Equal("\"v2\"", response.ETag);
            // The validator is what makes a poll free; the token is what makes it
            // survive a shared egress address.
            Assert.Equal("W/\"v1\"", handler.RequestHeaders["If-None-Match"]);
            Assert.Equal("Bearer fixture-token", handler.RequestHeaders["Authorization"]);
            Assert.Contains("GPT-Register-Tool/", handler.RequestHeaders["User-Agent"], StringComparison.Ordinal);
            Assert.Contains("application/vnd.github+json", handler.RequestHeaders["Accept"], StringComparison.Ordinal);
        }
        finally
        {
            Environment.SetEnvironmentVariable(GitHubReleaseFeed.TokenEnvironmentVariable, previous);
        }
    }

    [Fact]
    public async Task A_304_comes_back_as_not_modified_with_its_validator()
    {
        var handler = new CapturingHandler(_ =>
        {
            var response = new HttpResponseMessage(HttpStatusCode.NotModified);
            response.Headers.ETag = new EntityTagHeaderValue("\"v3\"");
            return response;
        });
        using var egress = new StubEgress(handler);
        var feed = new GitHubReleaseFeed(egress);

        ReleaseFeedResponse response = await feed.GetLatestReleaseJsonAsync("\"v2\"", CancellationToken.None);

        Assert.True(response.NotModified);
        Assert.Empty(response.Json);
        Assert.Equal("\"v3\"", response.ETag);
        Assert.Equal("\"v2\"", handler.RequestHeaders["If-None-Match"]);
    }

    private static HttpResponseMessage Refusal(
        HttpStatusCode status,
        params (string Name, string Value)[] headers)
    {
        var response = new HttpResponseMessage(status);
        foreach ((string name, string value) in headers)
        {
            // X-RateLimit-* have no typed accessor; Retry-After does, and the
            // typed property still parses a value added this way.
            response.Headers.TryAddWithoutValidation(name, value);
        }

        return response;
    }

    private static HttpResponseMessage Json(string body, string etag)
    {
        var response = new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(body) };
        if (etag.Length > 0)
            response.Headers.ETag = new EntityTagHeaderValue(etag);
        return response;
    }

    private sealed class CapturingHandler : HttpMessageHandler
    {
        private readonly Func<HttpRequestMessage, HttpResponseMessage> respond;

        public CapturingHandler(Func<HttpRequestMessage, HttpResponseMessage> respond)
            => this.respond = respond;

        public Dictionary<string, string> RequestHeaders { get; } = new(StringComparer.OrdinalIgnoreCase);

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request,
            CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            // Copied here: the feed disposes its request once the response lands.
            foreach (KeyValuePair<string, IEnumerable<string>> header in request.Headers)
                RequestHeaders[header.Key] = string.Join(",", header.Value);
            return Task.FromResult(respond(request));
        }
    }

    private sealed class StubEgress : IDesktopEgress, IDisposable
    {
        private readonly HttpClient client;

        public StubEgress(HttpMessageHandler handler) => client = new HttpClient(handler);

        public HttpClient CreateClient() => client;

        public bool IsAllowed(Uri? uri) => true;

        public void Dispose() => client.Dispose();
    }
}
