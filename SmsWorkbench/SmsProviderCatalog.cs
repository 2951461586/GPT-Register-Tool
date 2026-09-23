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
    /// default endpoint, the API-key environment variable name **or the
    /// protocol family**. Add a provider on one side without the other and that
    /// test goes red -- which is the only thing keeping a C#-only edit from
    /// silently pointing the one-click dialog at the wrong host, or at a host
    /// that does not speak the API it is about to call.
    /// </para>
    ///
    /// <para>
    /// `nexsms` speaks a different protocol family from the other three: REST
    /// under `/api/` with a `{code, message, data}` envelope and a lifecycle
    /// keyed on the phone number rather than an activation id. It is listed here
    /// because the Python side now ships a client for it
    /// (`sms_tool/nexsms.py`); before that it was a reserved name with
    /// `client_available = false`, and offering it would have put a
    /// selectable-but-broken choice in front of the operator.
    /// </para>
    ///
    /// <para>
    /// 🔴 <b>That protocol difference is carried in the table below</b> and must
    /// be, because the one-click dialog is *not* provider-agnostic: each family
    /// is read by a different reader in `SmsProviderCatalogClient`. The
    /// sms-activate rows use `?action=getCountries` / `getPricesV3` and a
    /// balance reply that starts with `ACCESS_BALANCE:`; the `nexsms` row uses
    /// REST paths under its own `/api/` and a `{code, message, data}` envelope,
    /// and answers every `?action=` call with `403 Forbidden`. `CatalogIsSmsActivate`
    /// is what the dialog dispatches on, and
    /// `tests/test_settings_catalog_provider_parity.py` pins every row's
    /// `Protocol` against `sms_tool/sms_providers.py` -- the same
    /// no-compiler-between-the-two-languages argument as the endpoint and
    /// API-key-env columns.
    /// </para>
    /// </summary>
    internal static class SmsProviderCatalog
    {
        internal sealed record SmsProvider(
            string Key, string Label, string DefaultEndpoint, string ApiKeyEnv, string Protocol)
        {
            /// <summary>
            /// Whether this provider's catalog is read over the sms-activate
            /// handler API (`?action=getCountries` / `getPricesV3`, balance via
            /// `ACCESS_BALANCE:`).
            ///
            /// <para>
            /// 🔴 This is **not** "can the dialog read this provider's catalog".
            /// Both families are readable online -- `false` here means the other
            /// reader is the right one (`nexsms_json`, which is REST under
            /// `/api/`), not that the provider is unsupported or that the dialog
            /// will fall back to the config. The backend rents from either
            /// family regardless.
            /// </para>
            /// </summary>
            internal bool CatalogIsSmsActivate => Protocol == SmsActivateProtocol;
        }

        internal const string DefaultKey = "smsbower";

        //: Protocol family keys. Byte-identical to `sms_providers.PROTOCOL_*`;
        //: the parity test asserts both the table column and these constants.
        internal const string SmsActivateProtocol = "sms_activate_handler";
        internal const string NexsmsProtocol = "nexsms_json";

        internal static readonly IReadOnlyList<SmsProvider> Providers = new[]
        {
            new SmsProvider(
                "smsbower", "SMSBower",
                "https://smsbower.page/stubs/handler_api.php", "SMSBOWER_API_KEY",
                SmsActivateProtocol),
            new SmsProvider(
                "herosms", "HeroSMS",
                "https://hero-sms.com/stubs/handler_api.php", "HEROSMS_API_KEY",
                SmsActivateProtocol),
            new SmsProvider(
                "grizzly", "Grizzly SMS",
                "https://api.grizzlysms.com/stubs/handler_api.php", "GRIZZLY_API_KEY",
                SmsActivateProtocol),
            // Base host only, no handler path: this vendor's client appends its
            // own `/api/...` per call. A path copied from the rows above would
            // still pass the parity test's string comparison but every request
            // would 404.
            new SmsProvider(
                "nexsms", "NexSMS",
                "https://api.nexsms.net", "NEXSMS_API_KEY",
                NexsmsProtocol),
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
