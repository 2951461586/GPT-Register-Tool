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
    public sealed class ProxyPoolRow
    {
        public string Proxy { get; set; } = "";
        public string Country { get; set; } = "";
        public string Asn { get; set; } = "";
        public string Isp { get; set; } = "";
        public string NetworkType { get; set; } = "";
        public string Edge { get; set; } = "";
        public string Result { get; set; } = "";
        public string Latency { get; set; } = "";
        public string Quality { get; set; } = "";
        public bool Eligible { get; set; }
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
