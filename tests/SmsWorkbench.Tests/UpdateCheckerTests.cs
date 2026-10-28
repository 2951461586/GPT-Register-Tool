using SmsWorkbench;

namespace SmsWorkbench.Tests;

/// <summary>
/// The update check is advisory, so every path must end in a result: a thrown
/// error on start-up would be the one place a network problem could break the
/// window. These tests pin the three things that can silently go wrong —
/// calendar-version comparison, tolerating a release document, and the throttle
/// that keeps the unauthenticated GitHub quota alive across restarts.
/// </summary>
public sealed class UpdateCheckerTests
{
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

    [Fact]
    public void Display_marks_an_unstamped_build_as_dev()
    {
        // The published artefact is stamped by build_dotnet.ps1; a plain `dotnet
        // build` leaves 0.0.0, and claiming "v0.0.0" would read as a release.
        Assert.Equal(
            AppVersion.Current == AppVersion.Unknown ? "dev" : "v" + AppVersion.Current,
            AppVersion.Display);
    }

    private sealed class FakeReleaseFeed : IReleaseFeed
    {
        public string Json { get; set; } = "";

        public Exception? Failure { get; set; }

        public int Calls { get; private set; }

        public Task<string> GetLatestReleaseJsonAsync(CancellationToken cancellationToken)
        {
            Calls++;
            cancellationToken.ThrowIfCancellationRequested();
            return Failure is null ? Task.FromResult(Json) : Task.FromException<string>(Failure);
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
