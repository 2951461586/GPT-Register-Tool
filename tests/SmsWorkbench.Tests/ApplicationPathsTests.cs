using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class ApplicationPathsTests
{
    [Fact]
    public void FindsRepositoryRootFromPublishedDirectory()
    {
        string root = Path.Combine(Path.GetTempPath(), "smsworkbench-paths-" + Guid.NewGuid().ToString("N"));
        string published = Path.Combine(root, "dist", "net10");
        Directory.CreateDirectory(published);
        File.WriteAllText(Path.Combine(root, "chatgpt_phone_reg.py"), "");
        try
        {
            var paths = new ApplicationPaths(published);

            Assert.Equal(root, paths.RootDirectory);
            Assert.Equal(Path.Combine(root, "chatgpt_phone_reg.py"), paths.BackendScriptPath);
        }
        finally
        {
            Directory.Delete(root, true);
        }
    }
}
