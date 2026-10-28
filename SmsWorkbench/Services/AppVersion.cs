// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System.Reflection;

namespace SmsWorkbench
{
    /// <summary>
    /// The running build's version, read from the assembly that actually shipped.
    /// </summary>
    /// <remarks>
    /// <para>
    /// <c>Directory.Build.props</c> owns the number (<c>Version</c>, default
    /// <c>0.0.0</c>, stamped by <c>SmsWorkbench/build_dotnet.ps1 -Version</c>), so
    /// this class must not keep a second copy of it. It reads
    /// <see cref="AssemblyInformationalVersionAttribute"/> first — the SDK writes
    /// the <c>Version</c> property there — and falls back to
    /// <see cref="AssemblyName.Version"/> for a hand-built binary.
    /// </para>
    /// <para>
    /// <see cref="Display"/> answers <c>dev</c> for an unstamped build instead of
    /// pretending to be <c>v0.0.0</c>: a number nobody stamped is not a release,
    /// and the update check treats it as "older than anything upstream", which is
    /// the useful behaviour for a local build.
    /// </para>
    /// </remarks>
    public static class AppVersion
    {
        /// <summary>What an unstamped build reports; also the comparison floor.</summary>
        public const string Unknown = "0.0.0";

        /// <summary>Numeric version without a leading <c>v</c>, or <see cref="Unknown"/>.</summary>
        public static string Current { get; } = Read();

        /// <summary>Human label: <c>v2026.10.28</c>, or <c>dev</c> when unstamped.</summary>
        public static string Display => Current == Unknown ? "dev" : "v" + Current;

        /// <summary>
        /// Reduce any version spelling to the <c>MAJOR[.MINOR[.PATCH[.BUILD]]]</c>
        /// core: drop a leading <c>v</c>, then the build metadata (<c>+sha</c>) and
        /// any prerelease tail (<c>-rc1</c>), then redundant trailing zero segments.
        /// </summary>
        /// <remarks>
        /// The SDK appends <c>+&lt;commit&gt;</c> to the informational version in
        /// some build configurations, and a git tag carries the <c>v</c>; without
        /// this normalisation the header would render <c>v2026.10.28+3f2a1c9</c>
        /// and the comparison would parse a segment as <c>0</c>.
        /// </remarks>
        public static string Normalize(string? raw)
        {
            string value = (raw ?? "").Trim();
            if (value.Length == 0)
                return "";

            if (value[0] is 'v' or 'V')
                value = value[1..];

            int metadata = value.IndexOf('+');
            if (metadata >= 0)
                value = value[..metadata];

            int prerelease = value.IndexOf('-');
            if (prerelease >= 0)
                value = value[..prerelease];

            value = value.Trim();
            if (value.Length == 0)
                return "";

            // "2026.10.28.0" is the same release as "2026.10.28"; keep three
            // segments at least, so a two-segment version is not rewritten.
            string[] segments = value.Split('.');
            int keep = segments.Length;
            while (keep > 3 && segments[keep - 1] == "0")
                keep--;
            return keep == segments.Length ? value : string.Join('.', segments[..keep]);
        }

        private static string Read()
        {
            Assembly assembly = typeof(AppVersion).Assembly;

            string fromAttribute = Normalize(
                assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion);
            if (fromAttribute.Length > 0 && fromAttribute != Unknown)
                return fromAttribute;

            Version? assemblyVersion = assembly.GetName().Version;
            if (assemblyVersion is null)
                return Unknown;

            string fromAssembly = Normalize(
                $"{Math.Max(0, assemblyVersion.Major)}.{Math.Max(0, assemblyVersion.Minor)}.{Math.Max(0, assemblyVersion.Build)}");
            return fromAssembly.Length > 0 ? fromAssembly : Unknown;
        }
    }
}
