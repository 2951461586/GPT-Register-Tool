// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench.Tests;

/// <summary>
/// Repository files read by the source-level guards (line ceilings, module
/// presence, "this file must not reference WPF").
/// </summary>
/// <remarks>
/// Shared rather than copied per test: every guard has to resolve the repository
/// root the same way, and a second copy of the walk-up is exactly the kind of drift
/// these tests exist to catch. The build output lives under
/// <c>tests/SmsWorkbench.Tests/bin/&lt;config&gt;/&lt;tfm&gt;/</c>, so the root is the
/// first ancestor that holds <c>GPTRegisterTool.slnx</c>.
/// </remarks>
internal static class RepoFile
{
    public static string Read(string relativePath) => File.ReadAllText(Path.Combine(Root(), relativePath));

    // File.ReadAllLines, not ReadAllText followed by a manual split: a file that
    // ends with a newline would otherwise count one phantom empty line and make
    // every ceiling fail by exactly one.
    public static string[] ReadLines(string relativePath) => File.ReadAllLines(Path.Combine(Root(), relativePath));

    public static int LineCount(string relativePath) => ReadLines(relativePath).Length;

    /// <summary>Source text with comment lines removed, for "does the code reference X" checks.</summary>
    public static string CodeOnly(string relativePath)
    {
        return string.Join(
            Environment.NewLine,
            ReadLines(relativePath).Where(line => !line.TrimStart().StartsWith("//", StringComparison.Ordinal)));
    }

    private static string Root()
    {
        var directory = new DirectoryInfo(AppContext.BaseDirectory);
        while (directory != null && !File.Exists(Path.Combine(directory.FullName, "GPTRegisterTool.slnx")))
        {
            directory = directory.Parent;
        }
        if (directory == null)
        {
            throw new InvalidOperationException("GPTRegisterTool.slnx was not found above the test output directory");
        }
        return directory.FullName;
    }
}
