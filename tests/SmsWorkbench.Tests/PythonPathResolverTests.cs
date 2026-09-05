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
}
