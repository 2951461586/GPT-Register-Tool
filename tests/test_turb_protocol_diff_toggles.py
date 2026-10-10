"""The four offline landings from the 2026-10-10 turb protocol diff.

A function-by-function read of ``myfanhua/turb-gpt-free-register@ffcda12``
against this repo's protocol lane found four wire/flow differences that were
never controlled variables (``docs/audits/scan-2026-10-10-protocol-registration.md``
§D1-D4).  Each now has one default-**off** toggle and one pre-registered A/B:

* D1 ``registration.turb_signin_authorize_context`` -- turb's signin/authorize
  context (no ``device_id`` / passkey capabilities / ``ccaps`` on the signin
  query; ``ccaps="login_methods chatgpt_login_finalizer_v1"``,
  ``auth_return_target_category=chatgpt_home`` and ``ui_locales`` on the
  authorize URL).
* D2 ``registration.prime_password_page_fatal`` -- a password-page prime that
  does not reach ``/create-account/password`` aborts the attempt instead of
  continuing to ``user/register``.
* D3a ``registration.otp_navigation_headers`` -- ``sec-fetch-site`` /
  ``sec-fetch-user`` on the ``email-otp/send`` navigation.
* D3b ``registration.otp_validate_sentinel`` -- a fresh ``authorize_continue``
  Sentinel pair on ``email-otp/validate``.
* D4 ``registration.otp_external_url_branch`` -- skip ``create_account`` when
  the validated transaction already finished on an external/callback URL.

Every toggle is default off, so the first thing each class pins is that the
default path is unchanged.
"""

from __future__ import annotations

import unittest
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from sms_tool import auth_flow
from sms_tool import registration_otp_stages as otp_stages
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_protocol_helpers import registration_flag


# ── D1: turb signin / authorize context ──────────────────────────────────────


def _signin_params(url: str) -> dict[str, list[str]]:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(url).query, keep_blank_values=True)


class TurbSigninAuthorizeContextTests(unittest.TestCase):
    def _url(self, enabled: bool):
        with patch.object(auth_flow.steps, "_turb_signin_authorize_context_enabled", lambda: enabled):
            return auth_flow.steps._openai_signin_url(
                "https://chatgpt.com",
                "did-1",
                "logging-1",
                "user@example.com",
                screen_hint="login_or_signup",
                prompt="login",
            )

    def test_default_signin_query_is_unchanged(self):
        params = _signin_params(self._url(False))
        self.assertEqual(params["device_id"], ["did-1"])
        self.assertEqual(params["ext-passkey-client-capabilities"], ["11111"])
        self.assertEqual(params["ccaps"], ["login_methods"])

    def test_turb_signin_query_drops_device_id_passkey_and_ccaps(self):
        params = _signin_params(self._url(True))
        self.assertNotIn("device_id", params)
        self.assertNotIn("ext-passkey-client-capabilities", params)
        self.assertNotIn("ccaps", params)
        # The identity + shape fields survive.
        self.assertEqual(params["ext-oai-did"], ["did-1"])
        self.assertEqual(params["auth_session_logging_id"], ["logging-1"])
        self.assertEqual(params["login_hint"], ["user@example.com"])
        self.assertEqual(params["screen_hint"], ["login_or_signup"])
        self.assertEqual(params["prompt"], ["login"])

    def test_default_authorize_context_is_unchanged(self):
        with patch.object(auth_flow.steps, "_turb_signin_authorize_context_enabled", lambda: False):
            url = auth_flow.steps._ensure_authorize_context(
                "https://auth.openai.com/api/accounts/authorize?state=1",
                "did-1",
                "logging-1",
                "user@example.com",
            )
        params = _signin_params(url)
        self.assertEqual(params["ccaps"], ["login_methods"])
        self.assertEqual(params["ext-passkey-client-capabilities"], ["11111"])
        self.assertNotIn("auth_return_target_category", params)
        self.assertNotIn("ui_locales", params)

    def test_turb_authorize_context_swaps_ccaps_and_adds_the_return_target(self):
        with patch.object(auth_flow.steps, "_turb_signin_authorize_context_enabled", lambda: True):
            url = auth_flow.steps._ensure_authorize_context(
                "https://auth.openai.com/api/accounts/authorize?state=1&ext-passkey-client-capabilities=11111",
                "did-1",
                "logging-1",
                "user@example.com",
                ui_locales="en-US",
            )
        params = _signin_params(url)
        self.assertNotIn("ext-passkey-client-capabilities", params)
        self.assertEqual(params["ccaps"], ["login_methods chatgpt_login_finalizer_v1"])
        self.assertEqual(params["auth_return_target_category"], ["chatgpt_home"])
        self.assertEqual(params["ui_locales"], ["en-US"])

    def test_ui_locales_is_the_first_accept_language_tag(self):
        self.assertEqual(auth_flow.steps._ui_locales_from_headers({"Accept-Language": "ja-JP,ja;q=0.9"}), "ja-JP")
        self.assertEqual(auth_flow.steps._ui_locales_from_headers({}), "")
        self.assertEqual(auth_flow.steps._ui_locales_from_headers(None), "")

    def test_mechanism_line_is_registered_and_distinct(self):
        from scripts import registration_ab

        design = registration_ab.EXPERIMENTS["p1-13-turb-signin-authorize-context"]
        self.assertEqual(design["toggle"], "registration.turb_signin_authorize_context")
        markers = [d["mechanism"] for d in registration_ab.EXPERIMENTS.values() if d.get("mechanism")]
        self.assertEqual(len(markers), len(set(markers)))
        self.assertIn("Turb signin/authorize context", markers)


# ── D2: fatal password-page landing ──────────────────────────────────────────


class _FakeWorkflow:
    """Minimal ``self`` for the two handler methods under test."""

    def __init__(self, **runtime):
        self.runtime = SimpleNamespace(**runtime)
        self.config: dict = {}
        self.aborted: list[str] = []
        self.posted = False
        self.r = SimpleNamespace()

    def _password_lane_active(self) -> bool:
        return True

    def _post_user_register(self) -> None:
        self.posted = True

    def _abort(self, message: str):
        self.aborted.append(message)
        raise AssertionError(f"aborted: {message}")


class PrimePasswordPageFatalTests(unittest.TestCase):
    def _prime(self, *, fatal: bool, landing: str):
        with (
            patch.object(auth_flow.steps, "_prime_password_page_fatal_enabled", lambda: fatal),
            patch.object(auth_flow.steps, "_prime_navigation_headers_enabled", lambda: False),
            patch.object(auth_flow.deps, "_follow_continue_url", return_value=Mock(status_code=200, url=landing)),
            patch.object(auth_flow.signup, "_emit_operator_line") as emit,
        ):
            result = auth_flow.signup._prime_create_account_password_page(
                Mock(), "https://auth.openai.com", {}, "https://auth.openai.com/api/accounts/authorize"
            )
        return result, emit

    def test_a_wrong_landing_is_reported_and_not_fatal_by_default(self):
        result, emit = self._prime(fatal=False, landing="https://auth.openai.com/email-verification")
        self.assertFalse(result["ok"])
        self.assertFalse(result["fatal"])
        labels = [call.args[1] for call in emit.call_args_list]
        self.assertNotIn("  Password page landing is fatal", labels)

    def test_the_fatal_toggle_marks_the_result_and_emits_its_marker(self):
        result, emit = self._prime(fatal=True, landing="https://auth.openai.com/email-verification")
        self.assertFalse(result["ok"])
        self.assertTrue(result["fatal"])
        labels = [call.args[1] for call in emit.call_args_list]
        self.assertIn("  Password page landing is fatal", labels)

    def test_a_correct_landing_is_never_fatal(self):
        result, _emit = self._prime(fatal=True, landing="https://auth.openai.com/create-account/password")
        self.assertTrue(result["ok"])

    def test_user_register_aborts_only_when_the_prime_flagged_fatal(self):
        def _fake():
            return _FakeWorkflow(
                signup_state={"url": "https://auth.openai.com/api/accounts/authorize"},
                session=Mock(),
                auth_base="https://auth.openai.com",
                base_headers={},
                reg_response=Mock(status_code=200),
                reg_data={},
            )

        fake = _fake()
        fake.r._prime_create_account_password_page_enabled = lambda: True
        fake.r._prime_create_account_password_page = lambda *a, **k: {"ok": False, "fatal": False}
        cast(Any, RegistrationEmailWorkflow.user_register)(fake)
        self.assertTrue(fake.posted)
        self.assertEqual(fake.aborted, [])

        fake2 = _fake()
        fake2.r._prime_create_account_password_page_enabled = lambda: True
        fake2.r._prime_create_account_password_page = lambda *a, **k: {"ok": False, "fatal": True, "url": "u"}
        with self.assertRaises(AssertionError):
            cast(Any, RegistrationEmailWorkflow.user_register)(fake2)
        self.assertFalse(fake2.posted)
        self.assertTrue(fake2.aborted[0].startswith("password_page_not_reached:"))


# ── D3a / D3b / D4: the OTP stage ────────────────────────────────────────────


class _OtpStage:
    def __init__(self, config: dict, **runtime):
        self.runtime = SimpleNamespace(**runtime)
        self.config = config
        self.r = SimpleNamespace()
        self.aborted: list[str] = []

    def _password_lane_active(self) -> bool:
        return True

    def _abort(self, message: str):
        self.aborted.append(message)
        raise AssertionError(f"aborted: {message}")

    def _issue_sentinel(self, flow: str, *, force_fresh: bool = False):
        self.issued = (flow, force_fresh)
        return SimpleNamespace(token="tok-1", so_token="so-1")


def _send_stage(*, headers_enabled: bool):
    stage = _OtpStage(
        {"registration": {"otp_navigation_headers": headers_enabled}},
        reg_data={},
        session=Mock(),
        base_headers={"oai-device-id": "did-1", "Accept-Language": "en-US"},
        auth_base="https://auth.openai.com",
        registration_mode="password",
        signup_state={"url": "https://auth.openai.com/api/accounts/authorize"},
        otp_send_dump={},
        otp_issued_after=0,
    )
    stage.r._email_otp_send_url = lambda reg_data: "https://auth.openai.com/api/accounts/email-otp/send"
    stage.r._follow_continue_url = Mock(
        return_value=Mock(status_code=200, url="https://auth.openai.com/email-verification")
    )
    stage.r._fetch_client_auth_session_dump = lambda *a, **k: {}
    stage.r._json_or_raw = lambda *a, **k: {}
    otp_stages.send_email_otp(stage)
    return stage


class OtpNavigationHeadersTests(unittest.TestCase):
    def test_default_send_headers_are_unchanged(self):
        stage = _send_stage(headers_enabled=False)
        headers = stage.r._follow_continue_url.call_args.args[2]
        self.assertNotIn("sec-fetch-site", headers)
        self.assertNotIn("sec-fetch-user", headers)

    def test_toggle_adds_the_navigation_headers(self):
        stage = _send_stage(headers_enabled=True)
        headers = stage.r._follow_continue_url.call_args.args[2]
        self.assertEqual(headers["sec-fetch-site"], "same-origin")
        self.assertEqual(headers["sec-fetch-user"], "?1")
        # The base headers are not mutated in place.
        self.assertNotIn("sec-fetch-site", stage.runtime.base_headers)


def _validate_stage(*, sentinel_enabled: bool, branch_enabled: bool, otp_data: dict):
    stage = _OtpStage(
        {
            "registration": {
                "otp_validate_sentinel": sentinel_enabled,
                "otp_external_url_branch": branch_enabled,
            }
        },
        session=Mock(),
        auth_base="https://auth.openai.com",
        base_headers={},
        email_code="123456",
        sentinel_data={"sentinel_token": "cached"},
        otp_data={},
        otp_external_url="",
        email_cfg={},
        auth_flow_started=0,
        mailbox_service=Mock(),
    )
    stage.r._validate_email_otp = Mock(return_value=(True, otp_data))
    stage.r._is_wrong_email_otp_code = lambda data: False
    stage.r._fetch_client_auth_session_dump = lambda *a, **k: {}
    stage.r._follow_continue_url = Mock(return_value=Mock(status_code=200))
    otp_stages.validate_email_otp(stage)
    return stage


class OtpValidateSentinelTests(unittest.TestCase):
    def test_default_validate_sends_no_sentinel(self):
        stage = _validate_stage(sentinel_enabled=False, branch_enabled=False, otp_data={"page": {"type": "about_you"}})
        self.assertFalse(stage.r._validate_email_otp.call_args.kwargs["use_sentinel"])
        self.assertEqual(stage.r._validate_email_otp.call_args.kwargs["sentinel_data"], {"sentinel_token": "cached"})

    def test_toggle_mints_a_fresh_authorize_continue_pair(self):
        stage = _validate_stage(sentinel_enabled=True, branch_enabled=False, otp_data={"page": {"type": "about_you"}})
        self.assertEqual(stage.issued, ("authorize_continue", True))
        kwargs = stage.r._validate_email_otp.call_args.kwargs
        self.assertTrue(kwargs["use_sentinel"])
        self.assertEqual(kwargs["sentinel_data"], {"sentinel_token": "tok-1", "sentinel_so_token": "so-1"})

    def test_a_mint_failure_is_non_fatal(self):
        stage = _OtpStage(
            {"registration": {"otp_validate_sentinel": True}},
            session=Mock(),
            auth_base="https://auth.openai.com",
            base_headers={},
            email_code="123456",
            sentinel_data={"sentinel_token": "cached"},
            otp_data={},
            otp_external_url="",
            email_cfg={},
            auth_flow_started=0,
            mailbox_service=Mock(),
        )
        stage.r._validate_email_otp = Mock(return_value=(True, {"page": {"type": "about_you"}}))
        stage.r._is_wrong_email_otp_code = lambda data: False
        stage.r._fetch_client_auth_session_dump = lambda *a, **k: {}
        stage.r._follow_continue_url = Mock(return_value=Mock(status_code=200))
        stage._issue_sentinel = Mock(side_effect=RuntimeError("mint down"))
        otp_stages.validate_email_otp(stage)
        self.assertFalse(stage.r._validate_email_otp.call_args.kwargs["use_sentinel"])


class OtpExternalUrlBranchTests(unittest.TestCase):
    def test_default_never_sets_the_external_url(self):
        stage = _validate_stage(
            sentinel_enabled=False,
            branch_enabled=False,
            otp_data={
                "page": {"type": "external_url"},
                "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=x",
            },
        )
        self.assertEqual(stage.runtime.otp_external_url, "")

    def test_toggle_records_the_callback(self):
        stage = _validate_stage(
            sentinel_enabled=False,
            branch_enabled=True,
            otp_data={
                "page": {"type": "external_url"},
                "continue_url": "https://chatgpt.com/api/auth/callback/openai?code=x",
            },
        )
        self.assertEqual(stage.runtime.otp_external_url, "https://chatgpt.com/api/auth/callback/openai?code=x")

    def test_about_you_is_not_an_external_url(self):
        self.assertEqual(otp_stages._external_url_after_otp({"continue_url": "https://auth.openai.com/about-you"}), "")
        self.assertEqual(
            otp_stages._external_url_after_otp(
                {"page": {"type": "about_you"}, "continue_url": "https://auth.openai.com/about-you"}
            ),
            "",
        )
        self.assertEqual(otp_stages._external_url_after_otp(None), "")
        self.assertEqual(otp_stages._external_url_after_otp("nope"), "")

    def test_create_account_is_skipped_when_the_flag_is_set(self):
        fake = _FakeWorkflow(otp_external_url="https://chatgpt.com/api/auth/callback/openai?code=x", create_data={})
        fake.r.think_stage = Mock()
        cast(Any, RegistrationEmailWorkflow.create_account)(fake)
        self.assertTrue(fake.runtime.create_ok)
        self.assertEqual(
            fake.runtime.create_data,
            {"_external_url": "https://chatgpt.com/api/auth/callback/openai?code=x"},
        )
        fake.r.think_stage.assert_called_once_with("post_create_account")


# ── the shared flag parser + the five gates ──────────────────────────────────


class RegistrationFlagParserTests(unittest.TestCase):
    def test_truth_table(self):
        for value in (True, 1, "1", "true", "True", "yes", "Yes", "on"):
            with self.subTest(value=value):
                self.assertTrue(registration_flag({"registration": {"k": value}}, "k", False))
        for value in (False, 0, "0", "false", "False", "no", "No", "off"):
            with self.subTest(value=value):
                self.assertFalse(registration_flag({"registration": {"k": value}}, "k", True))

    def test_unknown_and_missing_answer_the_toggle_default(self):
        self.assertTrue(registration_flag({"registration": {"k": "maybe"}}, "k", True))
        self.assertFalse(registration_flag({"registration": {"k": "maybe"}}, "k", False))
        self.assertTrue(registration_flag({"registration": {}}, "k", True))

    def test_a_frozen_section_is_read(self):
        frozen = MappingProxyType({"k": True})
        self.assertTrue(registration_flag({"registration": frozen}, "k", False))

    def test_non_mapping_inputs_are_the_default(self):
        self.assertFalse(registration_flag(None, "k", False))
        self.assertFalse(registration_flag({"registration": "oops"}, "k", False))
        self.assertFalse(registration_flag({}, "k", False))

    def test_every_new_toggle_defaults_off_and_reads_a_frozen_section(self):
        keys = (
            "turb_signin_authorize_context",
            "prime_password_page_fatal",
            "otp_navigation_headers",
            "otp_validate_sentinel",
            "otp_external_url_branch",
        )
        for key in keys:
            with self.subTest(key=key):
                self.assertFalse(registration_flag({"registration": {}}, key, False))
                self.assertTrue(registration_flag({"registration": MappingProxyType({key: True})}, key, False))


class OtpStageGateTests(unittest.TestCase):
    def _stage(self, config):
        return SimpleNamespace(config=config)

    def test_otp_gates_read_the_frozen_section(self):
        frozen = MappingProxyType(
            {"otp_navigation_headers": True, "otp_validate_sentinel": False, "otp_external_url_branch": True}
        )
        stage = self._stage({"registration": frozen})
        self.assertTrue(otp_stages._otp_navigation_headers_enabled(stage))
        self.assertFalse(otp_stages._otp_validate_sentinel_enabled(stage))
        self.assertTrue(otp_stages._otp_external_url_branch_enabled(stage))

    def test_otp_gates_default_off(self):
        stage = self._stage({})
        self.assertFalse(otp_stages._otp_navigation_headers_enabled(stage))
        self.assertFalse(otp_stages._otp_validate_sentinel_enabled(stage))
        self.assertFalse(otp_stages._otp_external_url_branch_enabled(stage))

    def test_otp_gates_never_raise_on_a_broken_config(self):
        stage = self._stage(None)
        self.assertFalse(otp_stages._otp_navigation_headers_enabled(stage))
        self.assertFalse(otp_stages._otp_validate_sentinel_enabled(stage))
        self.assertFalse(otp_stages._otp_external_url_branch_enabled(stage))


if __name__ == "__main__":
    unittest.main()
