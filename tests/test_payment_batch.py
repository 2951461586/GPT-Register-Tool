import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sms_tool import payment_batch


class PaymentBatchTests(unittest.TestCase):
    def setUp(self):
        config = patch.object(payment_batch, "CFG", {})
        config.start()
        self.addCleanup(config.stop)
        canary_pause = patch.object(payment_batch, "_active_canary_pause", return_value={})
        canary_pause.start()
        self.addCleanup(canary_pause.stop)

    def test_batch_runs_jit_gate_and_reports_matrix_counts(self):
        auth = {
            "ok": True,
            "access_token": "secret-token",
            "auth_context": {"email": "hidden@example.com"},
            "probed": 1,
            "refreshed": False,
            "probe": {"status_code": 200},
        }
        payment = {
            "ok": True,
            "payment_method": "momo",
            "decision": "ready_with_qr",
            "amount_due": 0,
            "has_momo": True,
            "url": "https://payment.momo.vn/v2/gateway/pay?t=1",
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", return_value=payment), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "report.json"):
            report = payment_batch.run_payment_batch(
                ["A@example.com", "a@example.com"],
                payment_method="momo",
                workers=5,
                matrix={"cells": [{"name": "vn", "sample_size": 1}]},
            )
        self.assertEqual(report["counts"]["requested"], 1)
        self.assertEqual(report["counts"]["qr_ready"], 1)
        self.assertEqual(report["matrix"][0]["eligible"], 1)
        self.assertNotIn("access_token", report["results"][0]["auth"])
        self.assertNotIn("email", report["results"][0])

    def test_manual_access_token_uses_liveness_probe_without_persisted_auth(self):
        auth_probe = {"status_code": 200, "ok": True}
        payment = {"ok": True, "decision": "ready", "url": "https://pay.example.test/opaque"}
        progress_events = []
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", side_effect=AssertionError("persisted auth must not run")), \
             patch("sms_tool.account_liveness.probe_account_liveness", return_value=auth_probe) as probe, \
             patch.object(payment_batch, "generate_payment_link", return_value=payment) as generate, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "manual.json"):
            report = payment_batch.run_payment_batch(
                ["AT-1"], payment_method="momo", retries=0,
                access_tokens={"AT-1": "secret-manual-at"},
                progress=progress_events.append,
            )

        probe.assert_called_once()
        self.assertEqual(probe.call_args.args[0]["email"], "at-1")
        self.assertNotIn("secret-manual-at", str(report))
        self.assertEqual(generate.call_args.kwargs["access_token"], "secret-manual-at")
        self.assertTrue(progress_events[-1]["account_terminal"])
        self.assertEqual(progress_events[-1]["status"], "completed")

    def test_checkpoint_keeps_only_artifact_presence_without_mutating_report(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        payment = {
            "ok": True,
            "decision": "ready_with_qr",
            "url": "https://provider.example.test/pay/opaque-reference",
            "provider_redirect_url": "https://redirect.example.test/opaque-reference",
            "details": {
                "fallback_url": "https://fallback.example.test/opaque-reference",
                "qr_data": "gopay://pay/opaque-reference",
                "qr_path": "C:/private/payment.png",
            },
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", return_value=payment), \
             patch.object(payment_batch, "_record_canary_state", return_value={"paused": False}), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "canary.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="gopay", canary=1, retries=0,
            )
            checkpoint = json.loads((Path(tmp) / "canary.json").read_text(encoding="utf-8"))

        row = report["results"][0]
        self.assertEqual(row["url"], payment["url"])
        self.assertEqual(row["provider_redirect_url"], payment["provider_redirect_url"])
        self.assertEqual(row["details"]["fallback_url"], payment["details"]["fallback_url"])
        self.assertEqual(row["details"]["qr_data"], payment["details"]["qr_data"])
        persisted_row = checkpoint["results"][0]
        self.assertTrue(persisted_row["url_present"])
        self.assertTrue(persisted_row["provider_redirect_url_present"])
        self.assertTrue(persisted_row["details"]["fallback_url_present"])
        self.assertTrue(persisted_row["details"]["qr_data_present"])
        self.assertTrue(persisted_row["details"]["qr_path_present"])
        serialized = json.dumps(checkpoint)
        for artifact in (
            payment["url"], payment["provider_redirect_url"],
            payment["details"]["fallback_url"], payment["details"]["qr_data"],
            payment["details"]["qr_path"],
        ):
            self.assertNotIn(artifact, serialized)

    def test_explicit_missing_matrix_path_fails_instead_of_using_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-matrix.json"
            with self.assertRaisesRegex(ValueError, "does not exist"):
                payment_batch.load_payment_matrix(str(missing))

    def test_explicit_matrix_file_with_bad_json_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad-matrix.json"
            path.write_text("{not-json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid payment matrix JSON"):
                payment_batch.load_payment_matrix(path)

    def test_stage_proxies_rotate_sticky_session_for_each_account(self):
        base = "http://user-region-US-sid-Old12345-t-5:secret@proxy.example:443"
        values = {
            "checkout_proxy": base,
            "promotion_proxy": base,
            "stage_proxy_countries": {"checkout": "US", "promotion": "JP"},
        }
        with patch("sms_tool.proxy_entry._random_session_id", side_effect=["New11111", "New22222"]):
            result = payment_batch._cell_payment_kwargs(values, {}, base)

        self.assertIn("region-US-sid-New11111", result["checkout_proxy"])
        self.assertIn("region-JP-sid-New22222", result["promotion_proxy"])
        self.assertNotEqual(result["checkout_proxy"], base)

    def test_checkout_and_approve_proxy_pools_are_selected_independently_per_cell(self):
        values = {
            "checkout_proxy": "http://legacy-checkout:8080",
            "approve_proxy": "http://legacy-approve:8080",
            "checkout_proxy_pool": "http://checkout-a:8080,http://checkout-b:8080",
            "approve_proxy_pool": ["http://approve-a:8080", "http://approve-b:8080"],
            "stage_proxy_countries": {"checkout": "JP", "approve": "TR"},
        }
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return f"http://selected-{stage}:8080", [{"ok": True}]

        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
            result = payment_batch._cell_payment_kwargs(values, {}, "")

        self.assertEqual(result["checkout_proxy"], "http://selected-checkout:8080")
        self.assertEqual(result["approve_proxy"], "http://selected-approve:8080")
        self.assertEqual(result["provider_proxy"], "http://selected-checkout:8080")
        self.assertEqual(result["promotion_proxy"], "http://selected-approve:8080")
        self.assertEqual([item[2] for item in seen], ["checkout", "approve"])
        self.assertEqual(seen[0][1], "JP")
        self.assertEqual(seen[1][1], "TR")
        self.assertNotIn("checkout_proxy_pool", result)
        self.assertNotIn("approve_proxy_pool", result)

    def test_gopay_approve_pool_defaults_to_jp_but_matrix_country_wins(self):
        values = {
            "checkout_proxy_pool": ["http://checkout-a:8080"],
            "approve_proxy_pool": ["http://approve-a:8080"],
            "target_country": "ID",
        }
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return pool[0], [{"ok": True}]

        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
            payment_batch._cell_payment_kwargs(
                values,
                {"checkout_country": "ID"},
                "",
                payment_method="gopay",
            )

        self.assertEqual([item[1] for item in seen], ["ID", "JP"])
        seen.clear()
        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
            payment_batch._cell_payment_kwargs(
                values,
                {"checkout_country": "ID", "approve_country": "TR"},
                "",
                payment_method="gopay",
            )
        self.assertEqual([item[1] for item in seen], ["ID", "TR"])

    def test_gopay_matrix_approve_country_outside_allowlist_is_coerced_to_jp(self):
        values = {
            "checkout_proxy_pool": ["http://checkout-a:8080"],
            "approve_proxy_pool": ["http://approve-a:8080"],
            "target_country": "ID",
        }
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return pool[0], [{"ok": True}]

        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
            result = payment_batch._cell_payment_kwargs(
                values,
                {"checkout_country": "ID", "approve_country": "US"},
                "",
                payment_method="gopay",
            )

        self.assertEqual([item[1] for item in seen], ["ID", "JP"])
        self.assertEqual(result["stage_proxy_countries"]["approve"], "JP")

    def test_gopay_base_approve_country_kwarg_is_coerced_to_jp(self):
        values = {
            "checkout_proxy_pool": ["http://checkout-a:8080"],
            "approve_proxy_pool": ["http://approve-a:8080"],
            "target_country": "ID",
            "approve_country": "US",
        }
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return pool[0], [{"ok": True}]

        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
            result = payment_batch._cell_payment_kwargs(
                values,
                {"checkout_country": "ID"},
                "",
                payment_method="gopay",
            )

        self.assertEqual([item[1] for item in seen], ["ID", "JP"])
        self.assertEqual(result["approve_country"], "JP")

    def test_non_gopay_matrix_approve_country_is_not_coerced(self):
        values = {
            "checkout_proxy_pool": ["http://checkout-a:8080"],
            "approve_proxy_pool": ["http://approve-a:8080"],
            "target_country": "PH",
        }
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return pool[0], [{"ok": True}]

        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
            result = payment_batch._cell_payment_kwargs(
                values,
                {"checkout_country": "PH", "approve_country": "US"},
                "",
                payment_method="grabpay",
            )

        self.assertEqual([item[1] for item in seen], ["PH", "US"])
        self.assertEqual(result["stage_proxy_countries"]["approve"], "US")

    def test_gopay_batch_forwards_coerced_approve_country_to_payment_link(self):
        auth = {
            "ok": True,
            "access_token": "secret",
            "auth_context": {"registration_country": "ID"},
            "probed": 1,
        }
        matrix = {"cells": [{
            "name": "us-approve",
            "payment_method": "gopay",
            "checkout_country": "ID",
            "approve_country": "US",
        }]}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "load_account_seed", return_value=({}, "")), \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", return_value={"ok": True, "url": "https://example.test/pay"}) as generate, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "coerce.json"):
            report = payment_batch.run_payment_batch(
                ["coerce@example.com"],
                payment_method="gopay",
                matrix=matrix,
                retries=0,
            )

        self.assertTrue(report["results"][0]["ok"])
        self.assertEqual(
            generate.call_args.kwargs["stage_proxy_countries"]["approve"],
            "JP",
        )

    def test_proxy_pool_start_rotates_by_account_index(self):
        values = {
            "checkout_proxy_pool": ["http://checkout-a:8080", "http://checkout-b:8080"],
            "approve_proxy_pool": ["http://approve-a:8080", "http://approve-b:8080"],
            "stage_proxy_countries": {"checkout": "ID", "approve": "JP"},
        }
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return pool[0], [{"ok": True}]

        with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
            payment_batch._cell_payment_kwargs(values, {}, "", pool_index=1)

        self.assertEqual(seen[0][0], ["http://checkout-b:8080", "http://checkout-a:8080"])
        self.assertEqual(seen[1][0], ["http://approve-b:8080", "http://approve-a:8080"])

    def test_batch_reads_method_proxy_pools_from_protocol_config(self):
        auth = {
            "ok": True,
            "access_token": "secret",
            "auth_context": {"registration_country": "ID"},
            "probed": 1,
        }
        config = {
            "protocol_payments": {
                "methods": {
                    "gopay": {
                        "checkout_proxy": "http://legacy-checkout:8080",
                        "approve_proxy": "http://legacy-approve:8080",
                        "checkout_proxy_pool": ["http://checkout-a:8080"],
                        "approve_proxy_pool": ["http://approve-a:8080"],
                    },
                },
            },
        }
        seen = []

        def choose(pool, expected_country, stage, **_kwargs):
            seen.append((list(pool), expected_country, stage))
            return f"http://selected-{stage}:8080", [{"ok": True}]

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "CFG", config), \
             patch.object(payment_batch, "load_account_seed", return_value=({}, "")), \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", return_value={"ok": True, "url": "https://example.test/pay"}) as generate, \
             patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "pool.json"):
            report = payment_batch.run_payment_batch(
                ["pool@example.com"],
                payment_method="gopay",
                payment_kwargs={
                    "checkout_proxy": "http://legacy-checkout:8080",
                    "approve_proxy": "http://legacy-approve:8080",
                    "stage_proxy_countries": {"checkout": "ID", "approve": "TR"},
                },
                retries=0,
            )

        self.assertTrue(report["results"][0]["ok"])
        self.assertEqual([item[2] for item in seen], ["checkout", "approve"])
        self.assertEqual(seen[0][1], "ID")
        self.assertEqual(seen[1][1], "TR")
        self.assertEqual(generate.call_args.kwargs["checkout_proxy"], "http://selected-checkout:8080")

    def test_matrix_checkout_route_is_resolved_once_before_jit_and_reused_for_payment(self):
        base = "http://user-region-US-sid-Old12345-t-5:secret@proxy.example:443"
        resolved = "http://user-region-ID-sid-New11111-t-5:secret@proxy.example:443"
        events = []
        payment_results = [
            {
                "ok": False,
                "status": "timed_out",
                "decision": "transport_failed",
                "retryable": True,
            },
            {"ok": True, "decision": "ready", "url": "https://example.test/pay"},
        ]

        def load_seed(**_kwargs):
            events.append("seed")
            return {"registration_country": "ID"}, ""

        def rotate_proxy(value, country):
            events.append(("rotate", value, country))
            return resolved

        def ensure_auth(**kwargs):
            events.append(("auth", kwargs["proxy"]))
            return {
                "ok": True,
                "access_token": "secret",
                "auth_context": {"registration_country": "ID"},
                "probed": 1,
            }

        def generate(**kwargs):
            events.append(("payment", kwargs["proxy"], kwargs["checkout_proxy"]))
            return payment_results.pop(0)

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "load_account_seed", side_effect=load_seed), \
             patch.object(payment_batch, "ensure_payment_access_token", side_effect=ensure_auth), \
             patch.object(payment_batch, "generate_payment_link", side_effect=generate) as payment, \
             patch("sms_tool.paypal_proxy.rotate_proxy_session", side_effect=rotate_proxy) as rotate, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "route.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"],
                payment_method="gopay",
                proxy=base,
                matrix={"cells": [{
                    "name": "id_gopay",
                    "payment_method": "gopay",
                    "registration_country": "ID",
                    "checkout_country": "ID",
                }]},
                retries=1,
            )

        self.assertEqual(events[0], "seed")
        self.assertEqual(events[1], ("rotate", base, "ID"))
        self.assertEqual(events[2], ("auth", resolved))
        self.assertEqual(events[3:], [
            ("payment", resolved, resolved),
            ("payment", resolved, resolved),
        ])
        rotate.assert_called_once_with(base, "ID")
        self.assertEqual(payment.call_count, 2)
        self.assertTrue(report["results"][0]["ok"])
        self.assertEqual(report["results"][0]["matrix_cell"], "id_gopay")

    def test_probe_and_jit_share_explicit_checkout_route_without_rotation(self):
        batch_proxy = "http://batch.example:8080"
        checkout_route = "http://checkout.example:8080"
        auth = {
            "ok": True,
            "access_token": "secret",
            "auth_context": {},
            "probed": 1,
        }
        capability = {
            "ok": True,
            "status": "completed",
            "classification": "eligible",
            "eligible": True,
            "conclusive": True,
            "decision": "payment_method_available",
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "load_account_seed", return_value=({}, "")), \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth) as ensure, \
             patch.object(payment_batch, "probe_payment_method", return_value=capability) as probe, \
             patch("sms_tool.paypal_proxy.rotate_proxy_session") as rotate, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "probe-route.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"],
                payment_method="gcash",
                proxy=batch_proxy,
                payment_kwargs={"checkout_proxy": checkout_route},
                probe_only=True,
                retries=0,
            )

        self.assertEqual(ensure.call_args.kwargs["proxy"], checkout_route)
        self.assertEqual(probe.call_args.kwargs["proxy"], checkout_route)
        self.assertEqual(probe.call_args.kwargs["checkout_proxy"], checkout_route)
        rotate.assert_not_called()
        self.assertEqual(report["results"][0]["decision"], "payment_method_available")

    def test_conclusive_ineligible_result_is_not_retried(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        payment = {"ok": False, "decision": "account_trial_ineligible", "error": "no trial"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", return_value=payment) as generate, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "report.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="momo", retries=2,
            )
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(report["counts"]["trial_ineligible"], 1)

    def test_terminal_result_counts_preserve_unknown_cancelled_and_timeout(self):
        counts = payment_batch._batch_counts([
            {"status": "cancelled", "ok": False},
            {"status": "unknown", "ok": False, "retryable": False},
            {"status": "timed_out", "ok": False, "retryable": True},
        ], 3)

        self.assertEqual(counts["cancelled"], 1)
        self.assertEqual(counts["unknown"], 1)
        self.assertEqual(counts["timed_out"], 1)
        self.assertEqual(counts["retryable"], 1)

    def test_matrix_matches_payment_method_and_registration_country(self):
        auth = {
            "ok": True,
            "access_token": "secret",
            "auth_context": {"registration_country": "VN"},
            "probed": 1,
        }
        payment = {"ok": False, "decision": "account_trial_ineligible"}
        matrix = {"cells": [
            {"name": "kr-kakao", "payment_method": "kakao", "registration_country": "KR"},
            {"name": "vn-momo", "payment_method": "momo", "registration_country": "VN"},
        ]}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", return_value=payment), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "report.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="momo", matrix=matrix,
            )
        self.assertEqual(report["results"][0]["matrix_cell"], "vn-momo")

    def test_matrix_country_mismatch_stops_before_checkout(self):
        auth = {
            "ok": True,
            "access_token": "secret",
            "auth_context": {"registration_country": "US"},
            "probed": 1,
        }
        matrix = {"cells": [
            {"name": "vn-momo", "payment_method": "momo", "registration_country": "VN"},
        ]}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link") as generate, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "report.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="momo", matrix=matrix,
            )
        self.assertEqual(report["results"][0]["decision"], "matrix_registration_country_mismatch")
        generate.assert_not_called()

    def test_stable_batch_id_resumes_checkpointed_accounts(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        payment = {"ok": True, "decision": "ready_with_qr", "amount_due": 0, "has_momo": True,
                   "url": "https://payment.momo.vn/pay/1"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth) as ensure, \
             patch.object(payment_batch, "generate_payment_link", return_value=payment), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "resume.json"):
            first = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="momo", batch_id="resume",
            )
            checkpoint = json.loads((Path(tmp) / "resume.json").read_text(encoding="utf-8"))
            report = payment_batch.run_payment_batch(["a@example.com"], payment_method="momo", batch_id="resume")
        self.assertEqual(first["results"][0]["url"], payment["url"])
        self.assertNotIn("url", checkpoint["results"][0])
        self.assertTrue(checkpoint["results"][0]["url_present"])
        self.assertEqual(ensure.call_count, 1)
        self.assertEqual(report["status"], "finished")
        self.assertEqual(report["resumed"], 1)
        self.assertEqual(report["counts"]["link_ready"], 1)
        self.assertEqual(report["counts"]["qr_ready"], 1)

    def test_retryable_checkpoint_row_is_executed_again(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        retryable = {
            "ok": False,
            "status": "timed_out",
            "decision": "transport_failed",
            "error_stage": "checkout",
            "retryable": True,
        }
        success = {"ok": True, "url": "https://example.test/pay"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth) as ensure, \
             patch.object(payment_batch, "probe_payment_method", return_value={"ok": True, "eligible": True, "conclusive": True, "decision": "payment_method_available"}), \
             patch.object(payment_batch, "generate_payment_link", side_effect=[retryable, success]) as generate, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "retry.json"):
            payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="paypal", batch_id="retry", retries=0,
                resume_checkpoint=True,
            )
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="paypal", batch_id="retry", retries=0,
                resume_checkpoint=True,
            )

        self.assertEqual(ensure.call_count, 2)
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(report["resumed"], 0)
        self.assertTrue(report["results"][0]["ok"])

    def test_explicit_non_retryable_checkpoint_row_is_resumed(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        terminal = {
            "ok": False,
            "status": "failed",
            "decision": "payment_method_unavailable",
            "retryable": False,
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth) as ensure, \
             patch.object(payment_batch, "probe_payment_method", return_value={"ok": True, "eligible": True, "conclusive": True, "decision": "payment_method_available"}), \
             patch.object(payment_batch, "generate_payment_link", return_value=terminal) as generate, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "terminal.json"):
            payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="paypal", batch_id="terminal", retries=0,
                resume_checkpoint=True,
            )
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="paypal", batch_id="terminal", retries=0,
                resume_checkpoint=True,
            )

        self.assertEqual(ensure.call_count, 1)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(report["resumed"], 1)
        self.assertFalse(report["results"][0]["ok"])

    def test_probe_only_runs_checkout_capability_without_payment_link_generation(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        capability = {
            "ok": True,
            "status": "completed",
            "classification": "eligible",
            "eligible": True,
            "conclusive": True,
            "decision": "payment_method_available",
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "probe_payment_method", return_value=capability) as probe, \
             patch.object(payment_batch, "generate_payment_link") as generate, \
             patch.object(payment_batch, "_active_canary_pause") as active_pause, \
             patch.object(payment_batch, "_record_canary_state") as record_canary, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "probe.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="paypal", probe_only=True,
            )
        generate.assert_not_called()
        probe.assert_called_once()
        active_pause.assert_not_called()
        record_canary.assert_not_called()
        self.assertEqual(report["counts"]["authenticated"], 1)
        self.assertEqual(report["counts"]["attempted"], 0)
        self.assertEqual(report["counts"]["completed"], 0)
        self.assertEqual(report["counts"]["capability_probed"], 1)
        self.assertEqual(report["results"][0]["decision"], "payment_method_available")

    def test_probe_only_retries_only_classified_transient_failures(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        transient = {
            "ok": False,
            "status": "failed",
            "classification": "unknown",
            "decision": "transport_failed",
            "retryable": True,
        }
        completed = {
            "ok": True,
            "status": "completed",
            "classification": "ineligible",
            "eligible": False,
            "decision": "payment_method_unavailable",
            "retryable": False,
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "probe_payment_method", side_effect=[transient, completed]) as probe, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "probe-retry.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="gcash", probe_only=True, retries=1,
            )

        self.assertEqual(probe.call_count, 2)
        self.assertEqual(report["results"][0]["attempts"], 2)
        self.assertEqual(report["results"][0]["decision"], "payment_method_unavailable")

    def test_probe_only_canary_records_capability_state(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        capability = {
            "ok": False,
            "status": "unknown",
            "classification": "unknown",
            "eligible": None,
            "conclusive": False,
            "decision": "stripe_init_failed",
            "retryable": True,
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "probe_payment_method", return_value=capability), \
             patch.object(payment_batch, "generate_payment_link") as generate, \
             patch.object(payment_batch, "_record_canary_state", return_value={"paused": True}) as record_canary, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "probe-canary.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="paypal", probe_only=True, canary=1,
            )
        generate.assert_not_called()
        record_canary.assert_called_once()
        self.assertTrue(report["canary_state"]["paused"])

    def test_probe_checkpoint_is_not_reused_for_payment_execution(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        payment = {"ok": True, "url": "https://example.test/pay"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth) as ensure, \
             patch.object(payment_batch, "probe_payment_method", return_value={
                 "ok": True, "status": "completed", "classification": "eligible",
                 "eligible": True, "conclusive": True, "decision": "payment_method_available",
             }), \
             patch.object(payment_batch, "generate_payment_link", return_value=payment) as generate, \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "same-id.json"):
            payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="paypal", batch_id="same-id", probe_only=True,
            )
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="paypal", batch_id="same-id", probe_only=False,
            )
        self.assertEqual(ensure.call_count, 2)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(report["resumed"], 0)
        self.assertFalse(report["probe_only"])
        self.assertEqual(report["counts"]["link_ready"], 1)

    def test_report_recursively_redacts_proxy_credentials(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        payment = {
            "ok": False,
            "decision": "checkout_failed",
            "error": "connect http://user:pass@proxy.example:8080 failed",
            "detail": {"checkout_proxy": "http://user:pass@proxy.example:8080"},
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", return_value=payment), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "report.json"):
            report = payment_batch.run_payment_batch(["a@example.com"], payment_method="momo", retries=0)
        serialized = str(report)
        self.assertNotIn("user:pass", serialized)
        self.assertNotIn("checkout_proxy", serialized)

    def test_probe_and_extract_use_distinct_default_report_paths(self):
        self.assertEqual(payment_batch._report_path("run", probe_only=True).name, "run.probe.json")
        self.assertEqual(payment_batch._report_path("run", probe_only=False).name, "run.extract.json")

    def test_report_contains_mode_and_per_account_stage_timing(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", return_value={"ok": True, "url": "https://example.test/pay"}), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "extract.json"):
            report = payment_batch.run_payment_batch(["a@example.com"], payment_method="momo", retries=0)
        self.assertEqual(report["mode"], "extract")
        row = report["results"][0]
        self.assertIn("stage_timings_ms", row)
        self.assertGreaterEqual(row["total_duration_ms"], 0)
        self.assertEqual(row["last_failed_stage"], "")

    def test_adapter_progress_uses_canonical_batch_events_and_stage_timings(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        events = []

        def generate(**kwargs):
            kwargs["progress"]({"stage": "checkout", "status": "running", "detail": "start"})
            kwargs["progress"]({"stage": "checkout", "status": "completed"})
            kwargs["progress"]({"stage": "stripe_init", "state": "running"})
            return {"ok": True, "url": "https://example.test/pay"}

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", side_effect=generate), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "extract.json"):
            report = payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="momo", retries=0, progress=events.append,
            )

        adapter_event = next(event for event in events if event["stage"] == "stripe_init")
        self.assertEqual(adapter_event["operation"], "extract")
        self.assertEqual(adapter_event["batch_id"], report["batch_id"])
        self.assertEqual(adapter_event["account_ref"], report["results"][0]["account_ref"])
        self.assertIn("checkout", report["results"][0]["stage_timings_ms"])
        self.assertIn("stripe_init", report["results"][0]["stage_timings_ms"])

    def test_checkpoint_resume_replays_durable_desktop_events(self):
        auth = {"ok": True, "access_token": "secret", "auth_context": {}, "probed": 1}
        payment = {"ok": True, "url": "https://example.test/pay"}
        replayed = []
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(payment_batch, "ensure_payment_access_token", return_value=auth), \
             patch.object(payment_batch, "generate_payment_link", return_value=payment), \
             patch.object(payment_batch, "_report_path", return_value=Path(tmp) / "resume.json"), \
             patch.object(payment_batch, "_event_path", return_value=Path(tmp) / "resume.events.jsonl"):
            payment_batch.run_payment_batch(["a@example.com"], payment_method="momo", batch_id="resume")
            payment_batch.run_payment_batch(
                ["a@example.com"], payment_method="momo", batch_id="resume", progress=replayed.append,
            )

        self.assertTrue(replayed)
        self.assertTrue(any(event.get("replayed") for event in replayed))
        self.assertTrue(any(event.get("account_terminal") for event in replayed))


if __name__ == "__main__":
    unittest.main()
