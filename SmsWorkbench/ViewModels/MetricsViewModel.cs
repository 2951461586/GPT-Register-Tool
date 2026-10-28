// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// Live throughput / success-rate strip for the status bar.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The numbers come from the same <see cref="BackendProgressEvent"/> stream the task
    /// list already consumes: one terminal event per finished account, carrying the
    /// backend's own <c>status</c> (<c>success</c>/<c>failed</c>/<c>cancelled</c>) and the
    /// run's duration. Nothing is inferred from log prose.
    /// </para>
    /// <para>
    /// A cancelled account is dropped rather than counted as a failure: cancelling a
    /// batch must not make the remaining accounts look less reliable than they are.
    /// </para>
    /// <para>
    /// Everything is a rolling window over the last ten minutes, bucketed in ten-second
    /// slices for the sparkline. Time is passed in (<c>nowUtc</c>) instead of read from
    /// the clock so the window arithmetic is testable without sleeping.
    /// </para>
    /// </remarks>
    public sealed class MetricsViewModel : ObservableObject
    {
        internal static readonly TimeSpan HistoryWindow = TimeSpan.FromMinutes(10);
        internal static readonly TimeSpan BucketWidth = TimeSpan.FromSeconds(10);
        internal const int BucketCount = 60;
        private static readonly TimeSpan RateWindow = TimeSpan.FromMinutes(1);

        private readonly List<Attempt> samples = new();
        private IReadOnlyList<double> throughput = new double[BucketCount];

        private int attemptsPerMinute;
        private string summaryText = "";
        private IReadOnlyList<FailureSlice> failures = Array.Empty<FailureSlice>();
        private string failureText = "";
        private bool failureVisible;

        public MetricsViewModel()
        {
            Sample();
        }

        /// <summary>Attempts per ten-second bucket, oldest first. Drives the sparkline.</summary>
        public IReadOnlyList<double> Throughput
        {
            get => throughput;
            private set => SetProperty(ref throughput, value);
        }

        /// <summary>Attempts in the last minute — the rate an operator actually reads.</summary>
        public int AttemptsPerMinute
        {
            get => attemptsPerMinute;
            private set => SetProperty(ref attemptsPerMinute, value);
        }

        /// <summary>One localized line: rate, success share and mean duration.</summary>
        public string SummaryText
        {
            get => summaryText;
            private set => SetProperty(ref summaryText, value);
        }

        /// <summary>Failures in the window, most common class first.</summary>
        /// <remarks>
        /// The class names are the backend's own machine values
        /// (<c>otp_timeout</c>, <c>auth_state</c>, ...) and are deliberately not
        /// translated: they are diagnostic vocabulary that matches the log lines an
        /// operator greps, not UI copy.
        /// </remarks>
        public IReadOnlyList<FailureSlice> Failures
        {
            get => failures;
            private set => SetProperty(ref failures, value);
        }

        /// <summary>One localized line naming the failure split, empty when nothing failed.</summary>
        public string FailureText
        {
            get => failureText;
            private set => SetProperty(ref failureText, value);
        }

        /// <summary>False while no failure is in the window, so the strip can hide the slot.</summary>
        public bool FailureVisible
        {
            get => failureVisible;
            private set => SetProperty(ref failureVisible, value);
        }

        /// <summary>Records a finished account. Terminal events only; cancelled is ignored.</summary>
        public void Record(BackendProgressEvent? progressEvent, DateTime? nowUtc = null)
        {
            if (progressEvent is null || !progressEvent.Terminal) return;
            if (string.Equals(progressEvent.Status, "cancelled", StringComparison.OrdinalIgnoreCase)) return;

            DateTime now = nowUtc ?? DateTime.UtcNow;
            bool success = string.Equals(progressEvent.Status, "success", StringComparison.OrdinalIgnoreCase);
            samples.Add(new Attempt(
                now,
                success,
                Math.Max(0, progressEvent.DurationMs),
                progressEvent.FailureClass ?? ""));
            Sample(now);
        }

        /// <summary>Drops samples outside the window and recomputes every derived value.</summary>
        public void Sample(DateTime? nowUtc = null)
        {
            DateTime now = nowUtc ?? DateTime.UtcNow;
            DateTime cutoff = now - HistoryWindow;
            samples.RemoveAll(sample => sample.AtUtc < cutoff);

            double[] buckets = new double[BucketCount];
            foreach (Attempt attempt in samples)
            {
                double ageSeconds = (now - attempt.AtUtc).TotalSeconds;
                int bucket = (int)(ageSeconds / BucketWidth.TotalSeconds);
                if (bucket < 0 || bucket >= BucketCount) continue;
                // Oldest first: index 0 is the oldest bucket, the newest is on the right.
                buckets[BucketCount - 1 - bucket] += 1;
            }

            AttemptsPerMinute = samples.Count(attempt => now - attempt.AtUtc <= RateWindow);
            Throughput = buckets;

            int finished = samples.Count;
            int succeeded = samples.Count(attempt => attempt.Success);
            long[] durations = samples.Where(attempt => attempt.DurationMs > 0)
                .Select(attempt => attempt.DurationMs)
                .ToArray();

            string rate = Loc.F("吞吐 {0}/分", AttemptsPerMinute);
            string success = finished == 0
                ? Loc.T("成功率 -")
                : Loc.F("成功率 {0}", (100.0 * succeeded / finished).ToString("0", CultureInfo.InvariantCulture) + "%");
            string average = durations.Length == 0
                ? Loc.T("平均 -")
                : Loc.F("平均 {0}", FormatDuration(durations.Average()));
            SummaryText = rate + " · " + success + " · " + average;

            ApplyFailures();
        }

        /// <summary>Recomposes the localized summary after a language switch.</summary>
        public void RefreshLanguage() => Sample();

        /// <summary>
        /// Rebuilds the failure split from the same window the rate uses.
        /// </summary>
        /// <remarks>
        /// A class with no name (older backends, or a failure raised before the stage
        /// taxonomy existed) collapses into one localized bucket rather than an empty
        /// label: "unknown 4" still tells an operator where to look, a blank does not.
        /// </remarks>
        private void ApplyFailures()
        {
            FailureSlice[] slices = samples
                .Where(attempt => !attempt.Success)
                .GroupBy(attempt => string.IsNullOrWhiteSpace(attempt.FailureClass)
                    ? Loc.T("未知")
                    : attempt.FailureClass)
                .Select(group => new FailureSlice(group.Key, group.Count()))
                .OrderByDescending(slice => slice.Count)
                .ThenBy(slice => slice.FailureClass, StringComparer.Ordinal)
                .ToArray();

            Failures = slices;
            FailureVisible = slices.Length > 0;
            FailureText = slices.Length == 0
                ? ""
                : Loc.F("失败 {0}", samples.Count(attempt => !attempt.Success))
                    + "（" + string.Join(" · ", slices.Select(slice => slice.FailureClass + " " + slice.Count)) + "）";
        }

        internal static string FormatDuration(double milliseconds)
        {
            if (milliseconds < 1000) return milliseconds.ToString("0", CultureInfo.InvariantCulture) + "ms";
            if (milliseconds < 60_000) return (milliseconds / 1000).ToString("0.0", CultureInfo.InvariantCulture) + "s";
            return (milliseconds / 60_000).ToString("0.0", CultureInfo.InvariantCulture) + "m";
        }

        // Named Attempt, not Sample: the public Sample() method above is the clock
        // tick, and a nested type of the same name is a compile error (CS0102).
        private readonly record struct Attempt(DateTime AtUtc, bool Success, long DurationMs, string FailureClass);
    }
}
