// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch used to stay `annotations`.
#nullable enable

namespace SmsWorkbench.Tests;

/// <summary>
/// Line ceilings for the window partials, plus the structural pins each split wave
/// produced.
/// </summary>
public sealed class PartialSizeTests
{
    private static readonly (string Path, int MaxLines)[] Partials =
    {
        // W9 (2026-11-21): ShellViewModel + 5 Coordinators + 11 XAML dialog pairs
        // dropped the MainWindow partials by >1,250 lines.
        ("SmsWorkbench/Views/MainWindow.xaml.cs", 245),
        ("SmsWorkbench/Views/MainWindow.Register.cs", 235),
        ("SmsWorkbench/Views/MainWindow.Export.cs", 128),
        ("SmsWorkbench/Views/MainWindow.Tasks.cs", 332),
        ("SmsWorkbench/Views/MainWindow.Pools.cs", 55),
        ("SmsWorkbench/Views/MainWindow.SmsProvider.cs", 22),
        ("SmsWorkbench/Views/MainWindow.Helpers.cs", 455),
        ("SmsWorkbench/Views/MainWindow.Inbox.cs", 152),
        ("SmsWorkbench/Views/MainWindow.Detail.cs", 116),
        ("SmsWorkbench/Views/MainWindow.Theme.cs", 142),
        ("SmsWorkbench/Views/MainWindow.Shell.cs", 78),
        ("SmsWorkbench/Views/MainWindow.NavigationState.cs", 36),
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
        // W8 (2026-11-20): the two hand-built dialogs became XAML.
        "SmsWorkbench/Models/AccountDetailView.cs",
        "SmsWorkbench/Dialogs/AccountDetailDialog.xaml.cs",
        "SmsWorkbench/Dialogs/ExportFormatDialog.xaml.cs",
        // W9 (2026-11-21): ShellViewModel, 5 Coordinators, and all remaining dialogs in XAML.
        "SmsWorkbench/ViewModels/ShellViewModel.cs",
        "SmsWorkbench/Services/PoolRefreshCoordinator.cs",
        "SmsWorkbench/Services/ThemeCoordinator.cs",
        "SmsWorkbench/Services/BackendBatchCoordinator.cs",
        "SmsWorkbench/Services/RegistrationWorkflowCoordinator.cs",
        "SmsWorkbench/Services/AccountExportCoordinator.cs",
        "SmsWorkbench/Dialogs/InfoConfirmDialogWindow.xaml.cs",
        "SmsWorkbench/Dialogs/RegisterOptionsDialog.xaml.cs",
        "SmsWorkbench/Dialogs/SelectedRegisterOptionsDialog.xaml.cs",
        "SmsWorkbench/Dialogs/ScanOptionsDialog.xaml.cs",
        "SmsWorkbench/Dialogs/ExportCompleteDialog.xaml.cs",
        "SmsWorkbench/Dialogs/AccountScanResultDialog.xaml.cs",
        "SmsWorkbench/Dialogs/ImportTargetDialog.xaml.cs",
        "SmsWorkbench/Dialogs/InboxDialogWindow.xaml.cs",
        "SmsWorkbench/Dialogs/ChangeEmailDialog.xaml.cs",
        "SmsWorkbench/Dialogs/ShellSettingsDialog.xaml.cs",
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
    [InlineData("SmsWorkbench/Models/AccountDetailView.cs")]
    [InlineData("SmsWorkbench/ViewModels/ShellViewModel.cs")]
    [InlineData("SmsWorkbench/Services/PoolRefreshCoordinator.cs")]
    [InlineData("SmsWorkbench/Services/ThemeCoordinator.cs")]
    [InlineData("SmsWorkbench/Services/BackendBatchCoordinator.cs")]
    [InlineData("SmsWorkbench/Services/RegistrationWorkflowCoordinator.cs")]
    [InlineData("SmsWorkbench/Services/AccountExportCoordinator.cs")]
    public void TheExtractedServicesStayWindowIndependent(string relativePath)
    {
        string code = RepoFile.CodeOnly(relativePath);

        Assert.DoesNotContain("System.Windows", code, StringComparison.Ordinal);
        Assert.DoesNotContain("MainWindow", code, StringComparison.Ordinal);
    }
}
