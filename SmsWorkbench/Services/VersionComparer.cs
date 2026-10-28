// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// Compares release versions numerically, segment by segment.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Deliberately not <see cref="System.Version"/>: a calendar version such as
    /// <c>2026.10.28</c> parses fine, but a tag with a missing component
    /// (<c>v2026.10</c>) or one extra (<c>2026.10.28.1</c>) throws or compares
    /// wrongly, and an update checker must never throw on the *tag it just read*.
    /// Missing segments are compared as zero, so <c>2026.10</c> == <c>2026.10.0</c>.
    /// </para>
    /// <para>
    /// This is "is there something newer", not semver precedence: the one thing
    /// the desktop does with the answer is show a link. Prerelease tails are
    /// therefore stripped (see <see cref="AppVersion.Normalize"/>) rather than
    /// ordered.
    /// </para>
    /// </remarks>
    public static class VersionComparer
    {
        /// <summary>True when <paramref name="candidate"/> is strictly newer.</summary>
        public static bool IsNewer(string candidate, string current) => Compare(candidate, current) > 0;

        /// <summary>-1 / 0 / 1 for <paramref name="left"/> against <paramref name="right"/>.</summary>
        public static int Compare(string left, string right)
        {
            IReadOnlyList<int> a = ParseSegments(left);
            IReadOnlyList<int> b = ParseSegments(right);
            int length = Math.Max(a.Count, b.Count);
            for (int i = 0; i < length; i++)
            {
                int x = i < a.Count ? a[i] : 0;
                int y = i < b.Count ? b[i] : 0;
                if (x != y)
                    return x > y ? 1 : -1;
            }

            return 0;
        }

        /// <summary>
        /// Numeric segments of a version. A non-numeric segment counts as zero:
        /// the value came off the network, so an unparsable tail must degrade the
        /// comparison, not abort it.
        /// </summary>
        internal static IReadOnlyList<int> ParseSegments(string? value)
        {
            string normalized = AppVersion.Normalize(value);
            if (normalized.Length == 0)
                return Array.Empty<int>();

            string[] parts = normalized.Split('.');
            var segments = new int[parts.Length];
            for (int i = 0; i < parts.Length; i++)
            {
                segments[i] = int.TryParse(parts[i], NumberStyles.Integer, CultureInfo.InvariantCulture, out int parsed)
                    ? parsed
                    : 0;
            }

            return segments;
        }
    }
}
