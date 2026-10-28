// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    /// <summary>
    /// One proxy-pool row. The same shape renders all three admin views (list /
    /// connectivity test / residential screening); a field a given view cannot
    /// fill stays empty rather than being invented.
    /// </summary>
    public sealed class ProxyPoolRow : ObservableObject
    {
        private string networkType = "";
        private string edge = "";
        private string result = "";
        private string latency = "";
        private bool isTesting;

        // Identity fields: written once when the row is built by the list/test/screen
        // mappers, never by a measurement.
        public string Proxy { get; set; } = "";
        public string Country { get; set; } = "";
        public string Asn { get; set; } = "";
        public string Isp { get; set; } = "";
        public string Quality { get; set; } = "";
        public bool Eligible { get; set; }

        // Measured fields: the inline latency test rewrites these in place, so they
        // have to notify. The grid is not rebuilt for a one-row measurement -- that
        // would drop the operator's selection and scroll position.
        public string NetworkType
        {
            get => networkType;
            set => SetProperty(ref networkType, value ?? "");
        }

        public string Edge
        {
            get => edge;
            set => SetProperty(ref edge, value ?? "");
        }

        public string Result
        {
            get => result;
            set => SetProperty(ref result, value ?? "");
        }

        public string Latency
        {
            get => latency;
            set => SetProperty(ref latency, value ?? "");
        }

        /// <summary>True while this row's own measurement is in flight.</summary>
        public bool IsTesting
        {
            get => isTesting;
            set => SetProperty(ref isTesting, value);
        }
    }

    /// <summary>Inputs shared by the test and screen commands.</summary>
    public sealed record ProxyPoolRequest(
        string Lane,
        string PoolText,
        string Country,
        int Workers,
        double TimeoutSeconds,
        int QualityMin);
}
