"""The registration lane must be able to leave an OAuth refresh token behind.

Measured 2026-09-14 over ``runtime/accounts.sqlite3``: **0 of 1245** rows hold a
non-empty ``oauth_refresh_token`` -- ``refresh_token_status`` is ``no_rt`` for
every single one.  The recovery chain's first and only *free* strategy is
``oauth_refresh_token`` (no email code, no browser), so on every account it is
skipped and the chain pays an email code or a browser instead.  That is the
measured reason a single liveness check burns an OTP.

The plumbing for this was already present and disconnected in five places:

* ``RegistrationState.CODEX_OAUTH`` -- enum member, never referenced;
* the ``codex_oauth`` constructor argument -- accepted, then hard-normalised to
  ``False`` (``"keep the legacy argument accepted for callers, but normalize it
  away instead of entering a dead stage"``);
* ``RegistrationRuntimeState.oauth_result`` -- read by ``finalize``, never assigned;
* ``_oauth_result_summary`` -- only ever called on ``{}``;
* ``oauth_refresh_token`` itself -- read by ``finalize``, never assigned.

``obtain_oauth_refresh_token`` reconnects the *outcome* without re-entering the
retired stage: no ``_run_stage``, no new enum member, and ``self.codex_oauth``
stays exactly as normalised.  It is **off by default**
(``registration.obtain_refresh_token``), so the AT-only behaviour the lane was
normalised to remains byte-identical unless an operator opts in.
"""

import inspect
import unittest
from unittest.mock import Mock, patch

from sms_tool import registration, registration_handlers
from sms_tool.accounts.account_models import AccountSessionModel
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.registration_state import RegistrationStateMachine

_TOKENS = {
    "access_token": "codex_at",
    "id_token": "codex_id",
    "refresh_token": "rt_1234567890",
}
_OAUTH_OK = {"ok": True, "mode": "codex_oauth_pkce", "tokens": dict(_TOKENS)}


def _workflow(*, config=None, success=True, access_token="web_at"):
    operations = Mock()
    operations._sanitize_text.side_effect = lambda value: str(value)
    workflow = RegistrationEmailWorkflow(
        RegistrationStateMachine(lambda *args: None),
        operations=operations,
        persistence=Mock(),
        config=config,
    )
    state = workflow.runtime
    state.username = "user@example.com"
    state.success = success
    state.error = "" if success else "create_account_failed"
    state.access_token = access_token
    state.device_id = "device-id"
    state.proxy = "http://proxy:8080"
    state.auth_session = {"cookie_header": "__Secure-next-auth.session-token=jwe"}
    state.session = Mock(name="logged_in_session")
    return workflow


class RefreshTokenIsOptInTests(unittest.TestCase):
    """Default behaviour must not change -- the lane is deliberately AT-only."""

    def test_no_config_section_means_no_exchange(self):
        workflow = _workflow(config=None)
        with patch("sms_tool.registration_handlers.collect_codex_oauth_tokens") as collect:
            workflow.obtain_oauth_refresh_token()

        collect.assert_not_called()
        self.assertEqual(workflow.runtime.oauth_result, {})
        self.assertEqual(workflow.runtime.oauth_refresh_token, "")

    def test_an_explicit_false_means_no_exchange(self):
        workflow = _workflow(config={"registration": {"obtain_refresh_token": False}})
        with patch("sms_tool.registration_handlers.collect_codex_oauth_tokens") as collect:
            workflow.obtain_oauth_refresh_token()

        collect.assert_not_called()

    def test_a_non_mapping_registration_section_means_no_exchange(self):
        workflow = _workflow(config={"registration": "not-a-mapping"})
        with patch("sms_tool.registration_handlers.collect_codex_oauth_tokens") as collect:
            workflow.obtain_oauth_refresh_token()

        collect.assert_not_called()


class RefreshTokenExchangeTests(unittest.TestCase):
    def _enabled(self, **kwargs):
        return _workflow(
            config={"registration": {"obtain_refresh_token": True}}, **kwargs
        )

    def test_a_successful_exchange_stores_the_refresh_token(self):
        workflow = self._enabled()
        with patch(
            "sms_tool.registration_handlers.collect_codex_oauth_tokens", return_value=dict(_OAUTH_OK)
        ) as collect:
            workflow.obtain_oauth_refresh_token()

        self.assertEqual(collect.call_count, 1)
        self.assertEqual(workflow.runtime.oauth_refresh_token, "rt_1234567890")
        self.assertTrue(workflow.runtime.oauth_result.get("ok"))

    def test_the_live_session_is_reused_and_the_otp_is_not_forced(self):
        """The whole point of doing it here: a live session short-circuits the
        login stages, so no email code is spent.  Forcing the OTP login would
        pay exactly the code this is meant to save."""
        workflow = self._enabled()
        with patch(
            "sms_tool.registration_handlers.collect_codex_oauth_tokens", return_value=dict(_OAUTH_OK)
        ) as collect:
            workflow.obtain_oauth_refresh_token()

        kwargs = collect.call_args.kwargs
        self.assertIs(kwargs["session"], workflow.runtime.session)
        self.assertIs(kwargs["force_email_otp_login"], False)
        self.assertEqual(kwargs["proxy"], "http://proxy:8080")
        self.assertEqual(kwargs["data"]["email"], "user@example.com")
        self.assertEqual(
            kwargs["data"]["cookie_header"], "__Secure-next-auth.session-token=jwe"
        )

    def test_only_the_refresh_token_is_taken(self):
        """The Codex access token is scoped to the Codex client -- it is not a
        substitute for this run's ChatGPT web AT, which was just probed."""
        workflow = self._enabled()
        with patch(
            "sms_tool.registration_handlers.collect_codex_oauth_tokens", return_value=dict(_OAUTH_OK)
        ):
            workflow.obtain_oauth_refresh_token()

        self.assertEqual(workflow.runtime.access_token, "web_at")
        self.assertEqual(workflow.runtime.id_token, "")

    def test_the_summarised_payload_reports_the_refresh_token(self):
        """``finalize`` writes ``_oauth_result_summary(s.oauth_result)`` into the
        persisted ``codex_oauth`` field, so that is the real consumer."""
        workflow = self._enabled()
        with patch(
            "sms_tool.registration_handlers.collect_codex_oauth_tokens", return_value=dict(_OAUTH_OK)
        ):
            workflow.obtain_oauth_refresh_token()

        summary = registration._oauth_result_summary(workflow.runtime.oauth_result)
        self.assertIs(summary["has_refresh_token"], True)
        self.assertNotIn("tokens", summary)


class RefreshTokenNeverChangesTheOutcomeTests(unittest.TestCase):
    """It runs *after* ``_set_outcome``; it must not be able to alter the verdict."""

    def _enabled(self, **kwargs):
        return _workflow(
            config={"registration": {"obtain_refresh_token": True}}, **kwargs
        )

    def test_a_failed_exchange_leaves_success_and_error_untouched(self):
        workflow = self._enabled()
        with patch(
            "sms_tool.registration_handlers.collect_codex_oauth_tokens",
            return_value={"ok": False, "mode": "codex_oauth_pkce", "error": "nope"},
        ):
            workflow.obtain_oauth_refresh_token()

        self.assertTrue(workflow.runtime.success)
        self.assertEqual(workflow.runtime.error, "")
        self.assertEqual(workflow.runtime.oauth_refresh_token, "")

    def test_a_raising_exchange_is_contained(self):
        workflow = self._enabled()
        with patch(
            "sms_tool.registration_handlers.collect_codex_oauth_tokens",
            side_effect=RuntimeError("transport exploded"),
        ):
            workflow.obtain_oauth_refresh_token()

        self.assertTrue(workflow.runtime.success)
        self.assertFalse(workflow.runtime.oauth_result.get("ok"))
        self.assertIn("transport exploded", workflow.runtime.oauth_result.get("error", ""))

    def test_no_access_token_means_no_exchange(self):
        workflow = self._enabled(access_token="")
        with patch("sms_tool.registration_handlers.collect_codex_oauth_tokens") as collect:
            workflow.obtain_oauth_refresh_token()

        collect.assert_not_called()

    def test_a_failed_registration_means_no_exchange(self):
        workflow = self._enabled(success=False)
        with patch("sms_tool.registration_handlers.collect_codex_oauth_tokens") as collect:
            workflow.obtain_oauth_refresh_token()

        collect.assert_not_called()


class BothOrchestrationsAreWiredTests(unittest.TestCase):
    """The fresh path and the resumed path must stay paired.

    A step wired into only one of them is the failure mode this project has hit
    repeatedly (see the three call sites of the login probe): the run looks fine
    until it takes the other path.
    """

    def test_every_set_outcome_is_followed_by_the_exchange(self):
        source = inspect.getsource(registration_handlers)
        settled = source.count("self._set_outcome()")
        exchanged = source.count("self.obtain_oauth_refresh_token()")

        self.assertEqual(settled, 2)
        self.assertEqual(
            exchanged,
            settled,
            "every orchestration that settles the outcome must also offer the "
            "opt-in refresh-token exchange",
        )


class TheExchangeIsImportedAtModuleScopeTests(unittest.TestCase):
    """Pin the import *location*, because the patch surface depends on it.

    ``collect_codex_oauth_tokens`` is imported at module scope on purpose.  A
    function-body import is what the delayed-import ratchet counts, and adding
    one here pushed the tree's total to 407 against a 406 baseline.  Importing
    it at module scope also matches how the rest of the package already reaches
    this symbol (``accounts/account_scan.py``, ``codex_export.py``,
    ``workspace_scan.py``).

    The consequence is that patching ``sms_tool.codex_oauth.collect_codex_oauth_tokens``
    no longer reaches this call site -- ``registration_handlers`` holds its own
    binding.  That is why every test above patches
    ``sms_tool.registration_handlers.collect_codex_oauth_tokens``.  Without this
    test, moving the import back into the function body would break four
    exchange tests in a way that reads like a mock bug rather than a moved
    import.
    """

    def test_the_symbol_is_bound_on_the_handlers_module(self):
        self.assertTrue(
            hasattr(registration_handlers, "collect_codex_oauth_tokens"),
            "obtain_oauth_refresh_token must resolve collect_codex_oauth_tokens "
            "through the handlers module's own namespace",
        )

    def test_a_function_body_import_would_be_counted_by_the_ratchet(self):
        # The premise, asserted first: this is the import form the ratchet
        # exists to stop growing.  If it ever stops counting these, the reason
        # for the module-scope import is gone and this test should be revisited.
        import ast

        tree = ast.parse(inspect.getsource(registration_handlers))
        module_scope = {
            node.asname or node.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "codex_oauth"
            for node in node.names
        }
        self.assertIn("collect_codex_oauth_tokens", module_scope)


class TheRefreshTokenActuallyReachesStorageTests(unittest.TestCase):
    """The exchange is worthless if the token stops at ``s.oauth_refresh_token``.

    ``obtain_oauth_refresh_token`` sets a runtime field; the column that the
    recovery chain reads is filled by a different module.  These two tests pin
    both halves of that hop, because a token that never lands is exactly the
    failure this whole change exists to fix (measured 0/1245 accounts).
    """

    def test_finalize_publishes_the_field_into_the_payload(self):
        source = inspect.getsource(RegistrationEmailWorkflow.finalize)
        self.assertIn('"oauth_refresh_token": s.oauth_refresh_token', source)
        self.assertIn(
            '"refresh_token_status": "oauth_present" if s.oauth_refresh_token else "no_rt"',
            source,
        )

    def test_the_storage_mapping_carries_the_token_and_the_status(self):
        mapping = AccountSessionModel.from_value(
            {
                "email": "user@example.com",
                "oauth_refresh_token": "rt_1234567890",
                "refresh_token_status": "oauth_present",
            }
        ).to_storage_mapping()
        self.assertEqual(mapping.get("oauth_refresh_token"), "rt_1234567890")
        self.assertEqual(mapping.get("refresh_token_status"), "oauth_present")

    def test_an_account_without_a_token_still_says_so(self):
        # The measured baseline this change is meant to move: every registered
        # account reports no_rt because nothing ever wrote the field.
        mapping = AccountSessionModel.from_value(
            {"email": "user@example.com", "oauth_refresh_token": "", "refresh_token_status": "no_rt"}
        ).to_storage_mapping()
        self.assertEqual(mapping.get("oauth_refresh_token"), "")
        self.assertEqual(mapping.get("refresh_token_status"), "no_rt")


if __name__ == "__main__":
    unittest.main()
