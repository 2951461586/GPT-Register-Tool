import unittest
from unittest.mock import patch

from sms_tool import auth_headers
from sms_tool.auth_headers import (
    AUTH_IMPERSONATE,
    DEFAULT_SEC_CH_UA,
    DEFAULT_USER_AGENT,
    hardware_profile_for_family,
    openai_auth_headers,
    nextauth_headers,
    chatgpt_headers,
    set_fingerprint_device,
    set_fingerprint_geo,
    sentinel_fingerprint,
)
from sms_tool.fingerprint_pool import FingerprintPool
from sms_tool.error_classification import classify_error
from sms_tool.registration_policy import registration_retry_decision


class AuthHeadersAndClassificationTests(unittest.TestCase):
    def test_auth_headers_include_device_sentinel_and_trace(self):
        headers = openai_auth_headers(
            "did-1",
            referer="https://auth.openai.com/create-account",
            sentinel={"sentinel_token": "sentinel", "sentinel_so_token": "so"},
            extra={"content-type": "application/json"},
        )

        self.assertEqual(headers["oai-device-id"], "did-1")
        self.assertEqual(headers["Origin"], "https://auth.openai.com")
        self.assertEqual(headers["openai-sentinel-token"], "sentinel")
        self.assertEqual(headers["openai-sentinel-so-token"], "so")
        self.assertIn("traceparent", headers)
        self.assertIn("x-datadog-trace-id", headers)

    def test_auth_headers_derive_origin_from_extra_referer(self):
        headers = openai_auth_headers(
            "did-2",
            extra={"Referer": "https://auth.openai.com/email-verification"},
        )

        self.assertEqual(headers["Origin"], "https://auth.openai.com")
        self.assertEqual(headers["Referer"], "https://auth.openai.com/email-verification")

    def test_geo_profile_changes_locale_and_timezone(self):
        set_fingerprint_geo("JP")
        fingerprint = sentinel_fingerprint()
        self.assertEqual(fingerprint["timezone"], "Asia/Tokyo")
        self.assertEqual(fingerprint["lang"], "ja-JP")
        set_fingerprint_geo("US")

    def test_header_families_have_distinct_protocol_fields(self):
        nextauth = nextauth_headers("did", session_id="sid")
        chat = chatgpt_headers("did", session_id="sid")
        self.assertNotIn("traceparent", nextauth)
        self.assertNotIn("oai-client-build-number", nextauth)
        self.assertEqual(chat["oai-client-build-number"], "8370486")
        self.assertEqual(chat["oai-session-id"], "sid")

    def test_auth_browser_fingerprint_versions_are_consistent(self):
        if AUTH_IMPERSONATE.startswith("firefox"):
            version = AUTH_IMPERSONATE.removeprefix("firefox")
            self.assertIn(f"Firefox/{version}.", DEFAULT_USER_AGENT)
            # Firefox emits no Sec-CH-UA client hints; the profile value stays blank.
            self.assertEqual(DEFAULT_SEC_CH_UA, "")
        else:
            version = AUTH_IMPERSONATE.removeprefix("chrome")
            self.assertIn(f"Chrome/{version}.", DEFAULT_USER_AGENT)
            self.assertIn(f'v="{version}"', DEFAULT_SEC_CH_UA)

    def test_rotated_fingerprint_keeps_tls_and_client_hints_coherent(self):
        cfg = {"mode": "rotate", "profiles": ["chrome124", "chrome131"]}
        with patch.object(auth_headers, "_auth_fingerprint_config", return_value=cfg):
            first = auth_headers.select_auth_fingerprint(rotate=True)
            second = auth_headers.select_auth_fingerprint(rotate=True)
            headers = auth_headers.openai_auth_headers("did")

        self.assertNotEqual(first["name"], second["name"])
        version = second["impersonate"].removeprefix("chrome")
        self.assertIn(f"Chrome/{version}.", headers["User-Agent"])
        self.assertIn(f'v="{version}"', headers["sec-ch-ua"])
        auth_headers._AUTH_FINGERPRINT_LOCAL.profile_name = AUTH_IMPERSONATE

    def test_sentinel_fingerprint_matches_auth_profile(self):
        with patch.object(auth_headers, "current_auth_fingerprint", return_value={
            "name": "chrome146",
            "impersonate": "chrome146",
            "user_agent": "Mozilla/5.0 Chrome/146.0.0.0 Safari/537.36",
            "sec_ch_ua": '"Chromium";v="146"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"Windows"',
        }):
            fingerprint = sentinel_fingerprint()
        self.assertEqual(fingerprint["impersonate"], "chrome146")
        self.assertIn("Chrome/146.", fingerprint["user_agent"])
        self.assertEqual(fingerprint["navigator_platform"], "Win32")

    def test_device_profile_is_deterministic_per_account_and_differs_across(self):
        # Same device id → identical hardware/display readings every time, so a
        # single account looks like one stable machine across relogin/recovery.
        hardware_keys = (
            "screen", "hardware_concurrency", "device_memory",
            "device_pixel_ratio", "js_heap_size_limit",
        )
        set_fingerprint_device("device-alpha")
        first = {key: sentinel_fingerprint()[key] for key in hardware_keys}
        set_fingerprint_device("device-alpha")
        second = {key: sentinel_fingerprint()[key] for key in hardware_keys}
        self.assertEqual(first, second)

        # A different account must not reuse the same device silhouette.
        distinct = set()
        for seed in ("device-beta", "device-gamma", "device-delta", "device-epsilon"):
            set_fingerprint_device(seed)
            profile = sentinel_fingerprint()
            distinct.add(tuple(profile[key] for key in hardware_keys))
        self.assertGreater(len(distinct), 1)

        # Values stay within the realistic desktop pools (deviceMemory capped 8).
        set_fingerprint_device("device-alpha")
        profile = sentinel_fingerprint()
        self.assertIn(profile["screen"], auth_headers._SCREEN_CHOICES)
        self.assertIn(profile["device_memory"], (4, 8))
        self.assertEqual(profile["max_touch_points"], 0)
        set_fingerprint_device("")

    def test_error_classification_prioritizes_account_over_timeout_substring(self):
        self.assertEqual(classify_error("outlook otp timeout"), "mailbox")
        self.assertEqual(classify_error("[WinError 10060] connection timeout via proxy"), "network")
        self.assertEqual(classify_error({"error": "account_deactivated", "body": "timeout"}), "account")

    def test_mailbox_timeout_is_not_classified_as_dropped_account(self):
        self.assertEqual(classify_error("email_otp_poll_timeout"), "mailbox")

    def test_invalid_auth_step_is_not_classified_as_dropped_account(self):
        self.assertEqual(classify_error("create_account_failed:invalid_auth_step"), "auth_state")

    def test_cloudflare_page_wins_over_generic_signup_auth_state(self):
        self.assertEqual(classify_error("signup_auth_state: 403 Just a moment..."), "network")

    def test_rate_limit_wins_over_generic_signup_auth_state(self):
        self.assertEqual(
            classify_error("signup_auth_state: status 429 rate_limit_exceeded Too many requests"),
            "rate_limit",
        )

    def test_sentinel_extraction_failure_is_retryable_network_error(self):
        self.assertEqual(classify_error("sentinel_extract_failed"), "network")

    def test_internal_and_configuration_failures_are_not_unknown(self):
        self.assertEqual(
            classify_error("registration_internal_error:NameError: missing dependency"),
            "internal",
        )
        self.assertEqual(
            classify_error("identity_ready_transport:name 'get_device_context' is not defined"),
            "internal",
        )
        self.assertEqual(
            classify_error("unsupported_registration_driver:protocol"),
            "configuration",
        )


class WrappedTransportFailureClassificationTests(unittest.TestCase):
    """``registration_internal_error:`` must not outrank a real curl failure.

    Observed 2026-09-12 in wave2 (``runtime/_verify_wave2_run.log:411``)::

        [!] Registration failed for b23a6a8439c0dde5:
            registration_internal_error:RuntimeError:Failed to perform,
            curl: (35) BoringSSL SSL_connect: Connection closed abruptly ...

    Its own text classifies as ``network`` (retryable), but the
    ``registration_internal_error:`` wrapper is tested first and won, so the
    result was recorded ``internal`` -- not retryable.

    The demotion is deliberately narrow. Only a curl error code can prove "this
    is really a transport failure"; the generic network markers ("connection",
    "timeout", "proxy", "dns") also occur inside genuine Python exception text,
    and demoting those would turn real bugs into retryable network noise. The
    negative tests below are the ones that pin that restriction.
    """

    CURL_35 = (
        "registration_internal_error:RuntimeError:Failed to perform, "
        "curl: (35) BoringSSL SSL_connect: Connection closed abruptly "
        "(SSL_ERROR_SYSCALL; error queue empty) in connection to chatgpt.com:443."
    )
    CURL_28 = (
        "registration_internal_error:RuntimeError:Failed to perform, "
        "curl: (28) Operation timed out after 20003 milliseconds with 58036 bytes received."
    )

    def test_wrapped_curl_ssl_failure_classifies_as_retryable_network(self):
        self.assertEqual(classify_error(self.CURL_35), "network")
        self.assertTrue(registration_retry_decision(self.CURL_35).retryable)

    def test_wrapped_curl_timeout_classifies_as_retryable_network(self):
        self.assertEqual(classify_error(self.CURL_28), "network")
        self.assertTrue(registration_retry_decision(self.CURL_28).retryable)

    def test_bare_curl_failure_is_network_too(self):
        self.assertEqual(classify_error("Failed to perform, curl: (35) broken"), "network")

    def test_internal_error_without_a_curl_code_stays_internal(self):
        self.assertEqual(
            classify_error("registration_internal_error:NameError: missing dependency"),
            "internal",
        )

    def test_internal_error_mentioning_connection_stays_internal(self):
        """The false-positive guard: a NameError is a bug, not a network blip."""
        error = "registration_internal_error:NameError: name 'connection_pool' is not defined"
        self.assertEqual(classify_error(error), "internal")
        self.assertFalse(registration_retry_decision(error).retryable)

    def test_internal_error_mentioning_timeout_stays_internal(self):
        error = "registration_internal_error:AttributeError: 'X' object has no attribute 'timeout'"
        self.assertEqual(classify_error(error), "internal")

    def test_internal_error_mentioning_proxy_stays_internal(self):
        error = "registration_internal_error:TypeError: proxy() got an unexpected keyword argument"
        self.assertEqual(classify_error(error), "internal")

    def test_plain_typeerror_stays_internal(self):
        self.assertEqual(classify_error("registration_internal_error:TypeError: boom"), "internal")

    def test_curl_markers_are_derived_not_hardcoded(self):
        """A second, drifting copy of the marker list is how this check rots."""
        from sms_tool.error_classification import (
            CURL_TRANSPORT_MARKERS,
            NETWORK_ERROR_MARKERS,
        )

        self.assertTrue(CURL_TRANSPORT_MARKERS)
        self.assertEqual(
            CURL_TRANSPORT_MARKERS,
            tuple(m for m in NETWORK_ERROR_MARKERS if m.startswith("curl:")),
        )


class FamilyHardwareProfileTests(unittest.TestCase):
    """P1-1: platform-class fields come from a family table, not a hardcoded Win32.

    The two current profiles are both Windows desktop, so their output must stay
    byte-identical to the formerly-hardcoded values. The table also must let P0-2
    add non-desktop families (macOS/iOS Safari) without creating a UA/platform
    contradiction.
    """

    def tearDown(self):
        auth_headers._AUTH_FINGERPRINT_LOCAL.profile_name = AUTH_IMPERSONATE

    def test_current_profiles_keep_windows_desktop_constants(self):
        # Regression guard: firefox/chrome must still report the old Win32 shape.
        for name in ("firefox144", "chrome146"):
            auth_headers._AUTH_FINGERPRINT_LOCAL.profile_name = name
            fp = sentinel_fingerprint()
            self.assertEqual(fp["navigator_platform"], "Win32")
            self.assertEqual(fp["max_touch_points"], 0)
            self.assertEqual(fp["sec_ch_ua_platform_version"], "10.0.0")
        # Only Chrome carries the Google vendor; Firefox's vendor is empty.
        auth_headers._AUTH_FINGERPRINT_LOCAL.profile_name = "firefox144"
        self.assertEqual(sentinel_fingerprint()["navigator_vendor"], "")
        auth_headers._AUTH_FINGERPRINT_LOCAL.profile_name = "chrome146"
        self.assertEqual(sentinel_fingerprint()["navigator_vendor"], "Google Inc.")

    def test_hardware_profile_for_family_returns_full_keys(self):
        firefox = hardware_profile_for_family("firefox144")
        chrome = hardware_profile_for_family("chrome146")
        for key in ("navigator_platform", "navigator_vendor", "max_touch_points", "sec_ch_ua_platform_version"):
            self.assertIn(key, firefox)
            self.assertIn(key, chrome)
        self.assertEqual(firefox["navigator_platform"], "Win32")
        self.assertEqual(chrome["navigator_vendor"], "Google Inc.")

    def test_unknown_family_falls_back_to_self_consistent_desktop(self):
        # A misclassified impersonate token must degrade to a populated desktop
        # profile, never to a half-empty dict that would drop required keys.
        unknown = hardware_profile_for_family("netscape-1.0")
        self.assertEqual(set(unknown.keys()), {
            "navigator_platform", "navigator_vendor", "max_touch_points", "sec_ch_ua_platform_version",
        })
        self.assertEqual(unknown["navigator_platform"], "Win32")

    def test_non_desktop_family_picks_up_own_platform_without_contradiction(self):
        # Contract for P0-2: once a macOS/iOS family is added to the table, the
        # sentinel fingerprint must report that platform instead of Win32, so a
        # Safari UA never coexists with a Windows navigator.platform. The platform
        # class ("macos") drives platform/touch/version; the browser family
        # ("safari") drives the vendor.
        mac_entry = {
            "navigator_platform": "MacIntel",
            "max_touch_points": 0,
            "sec_ch_ua_platform_version": "14.5.0",
        }
        with patch.dict(auth_headers._HARDWARE_PROFILES, {"macos": mac_entry}):
            with patch.object(auth_headers, "current_auth_fingerprint", return_value={
                "name": "safari17_0",
                "impersonate": "safari17_0",
                "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/17.0",
                "sec_ch_ua": "", "sec_ch_ua_mobile": "?0", "sec_ch_ua_platform": '"macOS"',
            }):
                fp = sentinel_fingerprint()
        self.assertEqual(fp["navigator_platform"], "MacIntel")
        self.assertEqual(fp["navigator_vendor"], "Apple Computer, Inc.")
        self.assertEqual(fp["sec_ch_ua_platform_version"], "14.5.0")
        # The per-account device silhouette is still derived independently.
        self.assertIn(fp["screen"], auth_headers._SCREEN_CHOICES)


class FingerprintPoolVersionMatrixTests(unittest.TestCase):
    """P0-2: protocol fingerprint pool becomes a weighted version matrix.

    Firefox stays the dominant share (ChatGPT's Cloudflare edge 403s Chrome), the
    two original profiles keep their canonical keys/values intact, and the pool
    supports both weighted-random (default) and round-robin (deterministic) modes.
    """

    def tearDown(self):
        auth_headers._AUTH_FINGERPRINT_LOCAL.profile_name = AUTH_IMPERSONATE

    def test_original_two_profiles_remain_byte_identical(self):
        # Canonical keys that drive persisted-account attribution must not change.
        self.assertEqual(
            auth_headers.AUTH_FINGERPRINT_PROFILES["firefox144"]["user_agent"],
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0",
        )
        self.assertEqual(
            auth_headers.AUTH_FINGERPRINT_PROFILES["chrome146"]["sec_ch_ua"],
            '"Chromium";v="146", "Google Chrome";v="146", "Not.A/Brand";v="99"',
        )

    def test_matrix_spans_firefox_chrome_safari_with_ios(self):
        fams = {}
        for name in auth_headers.AUTH_FINGERPRINT_PROFILES:
            fams.setdefault(auth_headers._browser_family(name), set()).add(name)
        self.assertIn("firefox", fams)
        self.assertIn("chrome", fams)
        self.assertIn("safari", fams)
        self.assertTrue(any(n.endswith("_ios") for n in fams["safari"]))

    def test_family_weights_keep_firefox_dominant(self):
        # With ~16 profiles the family shares must hold: Firefox majority, Chrome
        # minority, Safari present. Draw enough samples that the law of large
        # numbers pins the fractions well inside any plausible noise band.
        names = list(auth_headers.AUTH_FINGERPRINT_PROFILES)
        cfg = {"mode": "random", "profiles": names}
        counts = {"firefox": 0, "chrome": 0, "safari": 0}
        with patch.object(auth_headers, "_auth_fingerprint_config", return_value=cfg):
            for _ in range(1200):
                prof = auth_headers.select_auth_fingerprint(rotate=True)
                counts[auth_headers._browser_family(prof["impersonate"])] += 1
        total = sum(counts.values())
        ff, ch, sa = (counts[k] / total for k in ("firefox", "chrome", "safari"))
        self.assertGreater(ff, ch)
        self.assertGreater(ff, sa)
        self.assertGreater(ff, 0.40)
        self.assertGreater(sa, 0.0)

    def test_pool_default_is_weighted_random_and_round_robin_still_works(self):
        pool = FingerprintPool.from_config({})
        self.assertEqual(pool._mode, "random")
        self.assertGreater(pool.size, 2)
        rr = FingerprintPool(mode="round_robin")
        seen = {rr.next().name for _ in range(rr.size)}
        self.assertEqual(seen, {p.name for p in rr._profiles})

    def test_safari_sentinel_profile_reports_mac_platform(self):
        auth_headers._AUTH_FINGERPRINT_LOCAL.profile_name = "safari17_0"
        fp = sentinel_fingerprint()
        self.assertEqual(fp["navigator_platform"], "MacIntel")
        self.assertEqual(fp["navigator_vendor"], "Apple Computer, Inc.")
        self.assertEqual(fp["sec_ch_ua_platform_version"], "14.5.0")

    def test_select_by_exact_name_finds_profile(self):
        # P2-3: select(name) used to split on "_" and truncate, so
        # "safari18_0" became "safari18" which never matched any
        # profile name → always returned None.  Now the full name is
        # matched as-is.
        pool = FingerprintPool.from_config({})
        # Pick a real profile name from the pool to guarantee a match.
        real_name = pool._profiles[0].name
        found = pool.select(real_name)
        self.assertIsNotNone(found, f"select({real_name!r}) returned None")
        self.assertEqual(found.name, real_name)

        # A name with an underscore suffix must also match exactly.
        for p in pool._profiles:
            if "_" in p.name:
                result = pool.select(p.name)
                self.assertIsNotNone(result, f"select({p.name!r}) returned None")
                self.assertEqual(result.name, p.name)
                break

        # A non-existent name must still return None.
        self.assertIsNone(pool.select("nonexistent_profile_xyz"))


if __name__ == "__main__":
    unittest.main()
