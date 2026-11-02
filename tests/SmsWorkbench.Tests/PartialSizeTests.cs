// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench.Tests;

/// <summary>
/// Line ceilings for the window partials, plus the structural pins each split wave
/// produced.
/// </summary>
/// <remarks>
/// <para>
/// The C# side has no ratchet script like the Python tree's
/// <c>scripts/*_ratchet.py</c>, so the ceiling lives here, where <c>dotnet test</c>
/// already runs it. Each number is the file's size when its wave landed; it may only
/// go down. When a partial shrinks, lower its number in the same commit -- that is
/// what stops <c>MainWindow.Register.cs</c> from quietly regrowing to the 721 lines
/// it had before the dialogs and the batch-file builder moved out.
/// </para>
/// <para>
/// A line count is a blunt instrument: it cannot say "this file does too much". It is
/// used deliberately, only for the partials already known to be too big, and it is
/// cheap enough that nobody will want to disable it. There is deliberately no "sum of
/// all partials" test: every partial already has its own ceiling, so the sum could
/// only fail through a new file, and a new partial is a review question.
/// </para>
/// </remarks>
public sealed class PartialSizeTests
{
    private static readonly (string Path, int MaxLines)[] Partials =
    {
        // 367 -> 374 -> 376: the composition root wires each extracted
        // collaborator. It is allowed to grow with the services it builds; the
        // ceiling still stops anything else from doing so.
        ("SmsWorkbench/Views/MainWindow.xaml.cs", 383),
        ("SmsWorkbench/Views/MainWindow.Register.cs", 402),
        ("SmsWorkbench/Views/MainWindow.Export.cs", 190),
        ("SmsWorkbench/Views/MainWindow.Tasks.cs", 533),
        ("SmsWorkbench/Views/MainWindow.Pools.cs", 385),
        ("SmsWorkbench/Views/MainWindow.SmsProvider.cs", 22),
        ("SmsWorkbench/Views/MainWindow.Helpers.cs", 455),
        ("SmsWorkbench/Views/MainWindow.Inbox.cs", 301),
        ("SmsWorkbench/Views/MainWindow.Detail.cs", 287),
    };

    /// <summary>What each split wave produced.</summary>
    private static readonly string[] ExtractedModules =
    {
        // W1: registration
        "SmsWorkbench/Services/RegistrationRequestBuilder.cs",
        "SmsWorkbench/Models/RegistrationModels.cs",
        "SmsWorkbench/Dialogs/RegisterDialogService.cs",
        // W2: export
        "SmsWorkbench/Services/AccountExportAssembler.cs",
        "SmsWorkbench/Dialogs/ExportDialogService.cs",
        // W3/W4/W5: task delete, pool rows, SMS dialog
        "SmsWorkbench/Services/AccountDeleteService.cs",
        "SmsWorkbench/Services/AccountRowFactory.cs",
        "SmsWorkbench/Dialogs/SmsProviderDialogService.cs",
        // W6: backend run + accounts delta
        "SmsWorkbench/Services/BackendRunService.cs",
        "SmsWorkbench/Services/AccountRefreshPlan.cs",
        // W7: account detail + inbox
        "SmsWorkbench/Services/AccountDetailAssembler.cs",
        "SmsWorkbench/Services/InboxReader.cs",
        "SmsWorkbench/Services/MailboxRowClassifier.cs",
        "SmsWorkbench/Models/MailItem.cs",
    };

    public static TheoryData<string, int> PartialCeilings()
    {
        var data = new TheoryData<string, int>();
        foreach ((string path, int maxLines) in Partials)
            data.Add(path, maxLines);
        return data;
    }

    public static TheoryData<string> ExtractedModulePaths()
    {
        var data = new TheoryData<string>();
        foreach (string path in ExtractedModules)
            data.Add(path);
        return data;
    }

    [Theory]
    [MemberData(nameof(PartialCeilings))]
    public void APartialStaysWithinItsCeiling(string relativePath, int maxLines)
    {
        int actual = RepoFile.LineCount(relativePath);
        Assert.True(
            actual <= maxLines,
            $"{relativePath} grew to {actual} lines (ceiling {maxLines}). Split it instead of raising the ceiling.");
    }

    [Theory]
    [MemberData(nameof(ExtractedModulePaths))]
    public void TheExtractedModulesStillExist(string relativePath)
    {
        Assert.False(string.IsNullOrWhiteSpace(RepoFile.Read(relativePath)));
    }

    /// <summary>
    /// The whole point of a split: the rules are testable without a window. A
    /// <c>System.Windows</c> or <c>MainWindow</c> reference would undo that silently --
    /// it still compiles, it is just no longer the seam.
    /// </summary>
    [Theory]
    [InlineData("SmsWorkbench/Services/RegistrationRequestBuilder.cs")]
    [InlineData("SmsWorkbench/Services/AccountExportAssembler.cs")]
    [InlineData("SmsWorkbench/Services/AccountDeleteService.cs")]
    [InlineData("SmsWorkbench/Services/AccountRowFactory.cs")]
    [InlineData("SmsWorkbench/Services/BackendRunService.cs")]
    [InlineData("SmsWorkbench/Services/AccountRefreshPlan.cs")]
    [InlineData("SmsWorkbench/Services/AccountDetailAssembler.cs")]
    [InlineData("SmsWorkbench/Services/InboxReader.cs")]
    [InlineData("SmsWorkbench/Services/MailboxRowClassifier.cs")]
    public void TheExtractedServicesStayWindowIndependent(string relativePath)
    {
        // Only *code* counts: the XML docs of these files link back to the partial
        // they came from, and a `MainWindow` mention in prose is the opposite of a
        // leak -- it is the sentence that tells the next reader where the code lives.
        string code = RepoFile.CodeOnly(relativePath);

        Assert.DoesNotContain("System.Windows", code, StringComparison.Ordinal);
        Assert.DoesNotContain("MainWindow", code, StringComparison.Ordinal);
    }
}
