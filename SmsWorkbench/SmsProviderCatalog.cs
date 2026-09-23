// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System.Collections.Generic;
using System.Linq;

namespace SmsWorkbench
{
    /// <summary>
    /// SMS provider facts the desktop needs but cannot import from Python.
    ///
    /// <para>
    /// This is a hand-mirror of `sms_tool/sms_providers.py`, which is the single
    /// source of truth. Mirrors drift, so the two are pinned together by
    /// `tests/test_settings_catalog_provider_parity.py`: it extracts the table
    /// below from this file and fails on any difference in provider keys, the
    /// default endpoint or the API-key environment variable name. Add a provider
    /// on one side without the other and that test goes red -- which is the only
    /// thing keeping a C#-only edit from silently pointing the one-click dialog
    /// at the wrong host.
    /// </para>
    ///
    /// <para>
    /// `nexsms` is deliberately absent. The Python registry carries it as a
    /// reserved entry whose wire protocol is unverified (`client_available =
    /// false`), so it cannot serve a request; offering it here would put a
    /// selectable-but-broken choice in front of the operator.
    /// </para>
    /// </summary>
    internal static class SmsProviderCatalog
    {
        internal sealed record SmsProvider(string Key, string Label, string DefaultEndpoint, string ApiKeyEnv);

        internal const string DefaultKey = "smsbower";

        internal static readonly IReadOnlyList<SmsProvider> Providers = new[]
        {
            new SmsProvider(
                "smsbower", "SMSBower",
                "https://smsbower.page/stubs/handler_api.php", "SMSBOWER_API_KEY"),
            new SmsProvider(
                "herosms", "HeroSMS",
                "https://hero-sms.com/stubs/handler_api.php", "HEROSMS_API_KEY"),
            new SmsProvider(
                "grizzly", "Grizzly SMS",
                "https://api.grizzlysms.com/stubs/handler_api.php", "GRIZZLY_API_KEY"),
        };

        /// <summary>
        /// Descriptor for <paramref name="key"/>, falling back to the default
        /// provider when it is blank or unrecognised.
        ///
        /// An unrecognised key falls back rather than throwing on purpose: the
        /// config is user-editable and a stale value must not make the dialog
        /// unusable. The fallback is visible -- the operator sees the default
        /// provider's label and balance -- instead of an exception dialog.
        /// </summary>
        internal static SmsProvider Resolve(string? key)
        {
            string wanted = (key ?? "").Trim();
            return Providers.FirstOrDefault(
                       provider => string.Equals(provider.Key, wanted, System.StringComparison.OrdinalIgnoreCase))
                   ?? Providers.First(provider => provider.Key == DefaultKey);
        }

        /// <summary>Provider key as stored in `phone_reuse.source`.</summary>
        internal static string NormalizeKey(string? key) => Resolve(key).Key;

        /// <summary>Environment variable the Python side reads as a key fallback.</summary>
        internal static string ApiKeyEnv(string? key) => Resolve(key).ApiKeyEnv;
    }
}
