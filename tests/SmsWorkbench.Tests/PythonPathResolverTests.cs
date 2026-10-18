using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class PythonPathResolverTests
{
    [Fact]
    public void ResolvesRelativeInterpreterAgainstRepositoryRoot()
    {
        using var fixture = new TemporaryDirectory();
        var paths = new TestApplicationPaths(fixture.Path);

        string resolved = PythonPathResolver.Resolve(paths, ".venv/Scripts/python.exe");

        Assert.Equal(Path.Combine(fixture.Path, ".venv", "Scripts", "python.exe"), resolved);
    }

    [Fact]
    public void KeepsBarePythonCommandOnPath()
    {
        using var fixture = new TemporaryDirectory();
        var paths = new TestApplicationPaths(fixture.Path);

        Assert.Equal("python", PythonPathResolver.Resolve(paths, "python"));
        Assert.Equal("py", PythonPathResolver.Resolve(paths, "py"));
    }

    [Fact]
    public void PrefersTheBundledRuntimeWhenNoPathIsConfigured()
    {
        using var fixture = new TemporaryDirectory();
        var paths = new TestApplicationPaths(fixture.Path);
        string bundled = PythonPathResolver.BundledExecutable(paths);
        Directory.CreateDirectory(Path.GetDirectoryName(bundled)!);
        File.WriteAllText(bundled, "");

        Assert.Equal(bundled, PythonPathResolver.Resolve(paths, "python"));
        // An empty setting is the documented default, not an explicit choice.
        Assert.Equal(bundled, PythonPathResolver.Resolve(paths, ""));
    }

    [Fact]
    public void AnExplicitlyConfiguredInterpreterWinsOverTheBundledOne()
    {
        using var fixture = new TemporaryDirectory();
        var paths = new TestApplicationPaths(fixture.Path);
        string bundled = PythonPathResolver.BundledExecutable(paths);
        Directory.CreateDirectory(Path.GetDirectoryName(bundled)!);
        File.WriteAllText(bundled, "");

        string configured = Path.Combine(fixture.Path, ".venv", "Scripts", "python.exe");
        Assert.Equal(configured, PythonPathResolver.Resolve(paths, configured));
        Assert.Equal("py", PythonPathResolver.Resolve(paths, "py"));
    }
}
