using System.Text.RegularExpressions;

namespace SmsWorkbench.Tests;

/// <summary>
/// Pins which <see cref="BackendCommandPlanner"/> builders the desktop actually calls.
///
/// <para>
/// Why this exists: on 2026-10-25 six of the twenty builders had no production
/// caller at all — four of them because the buttons that used them were removed,
/// two because they had always been test-only. Nothing said so: the planner's
/// own tests exercised them, so the surface looked alive while the UI could not
/// reach it. That is how C#-side records of CLI flags rot: the flag disappears
/// from the Python parser, the planner test still passes because it asserts the
/// string it was handed, and nobody notices the contract is gone.
/// </para>
///
/// <para>
/// The six are kept on purpose rather than deleted: each one is the only place a
/// still-supported CLI contract (<c>--delete-account --email</c>,
/// <c>--refresh-session</c>, <c>--rebuild-sqlite</c>, the single-row
/// <c>--email-file</c> shape, the mailbox-free <c>--count/--workers</c> form and
/// the fixed failed-rerun argument set) is recorded on this side. They now carry
/// a "no production caller" doc comment, and this file freezes the set:
/// <list type="bullet">
///   <item>a <b>new</b> unreferenced builder fails — dead code has to be a decision;</item>
///   <item><b>wiring</b> one up fails until it is removed from the allowlist — the
///   annotation must not outlive the fact;</item>
///   <item><b>deleting</b> one fails the same way, so the CLI flag it pinned gets
///   removed from the Python parser in the same reviewed change.</item>
/// </list>
/// </para>
/// </summary>
public sealed class BackendCommandPlannerSurfaceTests
{
    /// <summary>
    /// Builders with no production caller. Every entry must carry a "no production
    /// caller" doc comment in <c>BackendCommandPlanner.cs</c> (asserted below).
    /// </summary>
    private static readonly string[] TestOnlyBuilders =
    [
        "CreateDeleteAccount",
        "CreatePoolRegistration",
        "CreateRebuildSqlite",
        "CreateRefreshSession",
        "CreateRerunFailedRegistration",
        "CreateSingleAccountImport",
    ];

    private const string PlannerRelativePath = "SmsWorkbench.Contracts/BackendCommandPlanner.cs";

    private static readonly Regex BuilderDeclaration = new(
        @"public static BackendCommandPlan (?<name>\w+)\(",
        RegexOptions.Compiled);

    private static readonly Regex BuilderCall = new(
        @"BackendCommandPlanner\.(?<name>\w+)\s*\(",
        RegexOptions.Compiled);

    [Fact]
    public void TheUnreferencedSet_IsExactlyTheFrozenAllowlist()
    {
        string[] unreferenced = AllBuilders(PlannerSource())
            .Where(name => !ReferencedBuilders().Contains(name))
            .OrderBy(name => name, StringComparer.Ordinal)
            .ToArray();

        Assert.Equal(TestOnlyBuilders.OrderBy(name => name, StringComparer.Ordinal).ToArray(), unreferenced);
    }

    [Fact]
    public void EveryTestOnlyBuilder_SaysSoInItsDocComment()
    {
        string source = PlannerSource();
        string[] lines = source.Replace("\r\n", "\n", StringComparison.Ordinal).Split('\n');
        var missing = new List<string>();

        foreach (string builder in TestOnlyBuilders)
        {
            int declaration = Array.FindIndex(
                lines,
                line => line.Contains("public static BackendCommandPlan " + builder + "(", StringComparison.Ordinal));
            Assert.True(declaration > 0, builder + " is not declared in " + PlannerRelativePath);

            // The doc block above the signature, not the whole file: a marker anywhere
            // else would let the annotation drift away from the builder it describes.
            string docBlock = string.Join(
                "\n",
                lines.Skip(Math.Max(0, declaration - 12)).Take(12));
            if (!docBlock.Contains("no production caller", StringComparison.Ordinal))
                missing.Add(builder);
        }

        Assert.True(
            missing.Count == 0,
            "these builders have no production caller but do not say so in their doc comment: "
                + string.Join(", ", missing));
    }

    [Fact]
    public void TheAllowlist_HasNoStaleEntry()
    {
        IReadOnlyList<string> declared = AllBuilders(PlannerSource());
        string[] stale = TestOnlyBuilders.Where(name => !declared.Contains(name)).ToArray();

        Assert.True(
            stale.Length == 0,
            "allow-listed builders that no longer exist (delete the entry, and remove the CLI flag "
                + "they pinned if nothing else asserts it): " + string.Join(", ", stale));
    }

    private static string PlannerSource() =>
        File.ReadAllText(Path.Combine(RepoRoot(), PlannerRelativePath.Replace('/', Path.DirectorySeparatorChar)));

    private static IReadOnlyList<string> AllBuilders(string plannerSource) =>
        BuilderDeclaration.Matches(plannerSource)
            .Select(match => match.Groups["name"].Value)
            .Distinct(StringComparer.Ordinal)
            .ToArray();

    /// <summary>
    /// Every <c>BackendCommandPlanner.Xxx(</c> call in production sources. Comment-only
    /// lines are skipped: two partials keep the removed action's builder name in a
    /// prose comment explaining why it is gone, and that is not a caller.
    /// </summary>
    private static HashSet<string> ReferencedBuilders()
    {
        var referenced = new HashSet<string>(StringComparer.Ordinal);
        foreach (string path in ProductionSources())
        {
            foreach (string line in File.ReadLines(path))
            {
                string trimmed = line.TrimStart();
                if (trimmed.StartsWith("//", StringComparison.Ordinal) || trimmed.StartsWith("*", StringComparison.Ordinal))
                    continue;
                foreach (Match match in BuilderCall.Matches(line))
                    referenced.Add(match.Groups["name"].Value);
            }
        }
        return referenced;
    }

    private static IEnumerable<string> ProductionSources()
    {
        string workbench = Path.Combine(RepoRoot(), "SmsWorkbench");
        return Directory.EnumerateFiles(workbench, "*.cs", SearchOption.AllDirectories)
            .Where(path => !path.Contains(Path.DirectorySeparatorChar + "obj" + Path.DirectorySeparatorChar, StringComparison.Ordinal)
                && !path.Contains(Path.DirectorySeparatorChar + "bin" + Path.DirectorySeparatorChar, StringComparison.Ordinal));
    }

    private static string RepoRoot()
    {
        DirectoryInfo? dir = new(AppContext.BaseDirectory);
        for (int i = 0; i < 10 && dir != null; i++, dir = dir.Parent)
        {
            if (File.Exists(Path.Combine(dir.FullName, PlannerRelativePath.Replace('/', Path.DirectorySeparatorChar))))
                return dir.FullName;
        }
        throw new InvalidOperationException("repo root not found from test output");
    }
}
