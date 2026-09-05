"""Behaviour tests for sms_tool/paypal_link/gen_link.py -- pure helpers (P1).

Context: ``gen_link.py`` (1255 lines) is *not* zero-coverage -- ``test_gen_pp_link.py``
drives ``generate_pp_link`` end to end with a fake session. What that file does
not touch is the **pure decision layer** underneath it:

* ``_normalized_generation_type`` / the four ``_is_*_generation_type`` predicates —
  these decide which of three money paths a request takes, and a typo in a config
  value silently falls through to the default path;
* the URL builders (``_canonical_checkout_long_url``, ``_chatgpt_checkout_url``,
  ``_normalize_hosted_checkout_url``);
* ``_checkout_country_from_cfg`` — the country feeds currency and billing address;
* ``_prepare_configured_stage_proxy`` — the preflight gate that decides whether
  a stage runs at all.

No network, no browser, no real money. Config is passed in as plain dicts; the
only patched boundaries are ``probe_proxy`` and ``rotate_proxy_session``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sms_tool.paypal_link import gen_link
from sms_tool.paypal_proxy import PayPalProxyState, ProxyProbeResult

# ────────────────────────────── generation-type normalisation ──────────────────────────────


class NormalizedGenerationTypeTests(unittest.TestCase):
    def test_override_wins_over_config(self):
        cfg = {"link_generation_type": "hosted_long_url"}
        self.assertEqual("pp_direct", gen_link._normalized_generation_type(cfg, "PP_Direct"))

    def test_config_keys_are_tried_in_order(self):
        """link_generation_type → generation_type → paypal_generation_type。"""
        self.assertEqual("first", gen_link._normalized_generation_type(
            {"link_generation_type": "first", "generation_type": "second",
             "paypal_generation_type": "third"}))
        self.assertEqual("second", gen_link._normalized_generation_type(
            {"generation_type": "second", "paypal_generation_type": "third"}))
        self.assertEqual("third", gen_link._normalized_generation_type(
            {"paypal_generation_type": "third"}))

    def test_case_and_hyphens_are_normalised(self):
        for raw, expected in [
            ("Hosted-Long-URL", "hosted_long_url"),
            ("  PP_DIRECT  ", "pp_direct"),
            ("ChatGPT-Checkout-Link", "chatgpt_checkout_link"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(expected, gen_link._normalized_generation_type({}, raw))

    def test_missing_everything_is_empty_string(self):
        self.assertEqual("", gen_link._normalized_generation_type({}))
        self.assertEqual("", gen_link._normalized_generation_type({}, None))

    def test_empty_override_falls_back_to_config(self):
        """空字符串 override 不算 override —— `or` 链会继续往配置里找。"""
        cfg = {"link_generation_type": "hosted"}
        self.assertEqual("hosted", gen_link._normalized_generation_type(cfg, ""))
        self.assertEqual("hosted", gen_link._normalized_generation_type(cfg, None))

    def test_whitespace_only_override_shadows_the_config(self):
        """⚠️ 钉住现状：只含空白的 override 是 truthy，`or` 链**不会**回落。

        结果被 strip 成空串 → 四个谓词全不匹配 → 静默走默认路径，
        配置里的 link_generation_type 被吞掉。传 " " 和传 "" 结果完全不同。
        要修语义就先改这个用例。
        """
        cfg = {"link_generation_type": "hosted"}
        self.assertEqual("", gen_link._normalized_generation_type(cfg, "   "))

    def test_non_string_config_value_is_stringified(self):
        self.assertEqual("7", gen_link._normalized_generation_type({"link_generation_type": 7}))


class GenerationTypePredicateTests(unittest.TestCase):
    """钉住四个谓词：一个配置值属于哪条 money path 全靠它们。"""

    PAYPAL_DIRECT = [
        "pp_direct", "paypal_direct", "direct_pp", "paypal_approve", "ba_direct", "ba_approve",
    ]
    ZERO_DUE = [
        "pp_direct_zero_due", "paypal_direct_zero_due", "direct_pp_zero_due",
        "paypal_approve_zero_due", "ba_direct_zero_due", "ba_approve_zero_due",
        "pp_direct_0_due", "paypal_direct_0_due",
        "pp_direct_force_zero", "paypal_direct_force_zero", "paypal_direct_require_zero_due",
    ]
    HOSTED = ["long", "long_link", "hosted", "hosted_long", "hosted_long_url",
              "stripe_hosted", "chatgpt_checkout"]
    CHATGPT_CHECKOUT_LINK = ["chatgpt_checkout_link", "checkout_link",
                             "short_checkout", "chatgpt_short_link"]

    def test_direct_aliases_are_recognised(self):
        for value in self.PAYPAL_DIRECT + self.ZERO_DUE:
            with self.subTest(value=value):
                self.assertTrue(gen_link._is_paypal_direct_generation_type(value))

    def test_zero_due_aliases_are_recognised(self):
        for value in self.ZERO_DUE:
            with self.subTest(value=value):
                self.assertTrue(gen_link._is_zero_due_generation_type(value))

    def test_plain_direct_is_not_zero_due(self):
        for value in self.PAYPAL_DIRECT:
            with self.subTest(value=value):
                self.assertFalse(gen_link._is_zero_due_generation_type(value))

    def test_hosted_aliases_are_recognised(self):
        for value in self.HOSTED:
            with self.subTest(value=value):
                self.assertTrue(gen_link._is_hosted_generation_type(value))

    def test_chatgpt_checkout_link_aliases_are_recognised(self):
        for value in self.CHATGPT_CHECKOUT_LINK:
            with self.subTest(value=value):
                self.assertTrue(gen_link._is_chatgpt_checkout_link_generation_type(value))

    def test_unknown_value_matches_nothing(self):
        for value in ("", "pp", "direct", "long_url", "stripe", "chatgpt_checkout_link_x"):
            with self.subTest(value=value):
                self.assertFalse(gen_link._is_paypal_direct_generation_type(value))
                self.assertFalse(gen_link._is_zero_due_generation_type(value))
                self.assertFalse(gen_link._is_hosted_generation_type(value))
                self.assertFalse(gen_link._is_chatgpt_checkout_link_generation_type(value))

    def test_the_four_buckets_are_disjoint(self):
        """一个值只能属于一条路径 —— 重合意味着分发顺序决定结果，很容易改坏。"""
        buckets = [
            {v for v in self.PAYPAL_DIRECT if gen_link._is_paypal_direct_generation_type(v)},
            set(self.HOSTED),
            set(self.CHATGPT_CHECKOUT_LINK),
        ]
        for i, a in enumerate(buckets):
            for j, b in enumerate(buckets):
                if i < j:
                    self.assertEqual(set(), a & b, f"bucket {i} overlaps bucket {j}")

    def test_similar_names_that_are_easy_to_confuse(self):
        """`chatgpt_checkout`（hosted）和 `chatgpt_checkout_link`（短链）只差一个词。"""
        self.assertTrue(gen_link._is_hosted_generation_type("chatgpt_checkout"))
        self.assertFalse(gen_link._is_chatgpt_checkout_link_generation_type("chatgpt_checkout"))
        self.assertTrue(gen_link._is_chatgpt_checkout_link_generation_type("chatgpt_checkout_link"))
        self.assertFalse(gen_link._is_hosted_generation_type("chatgpt_checkout_link"))


# ────────────────────────────── URL builders ──────────────────────────────


class UrlBuilderTests(unittest.TestCase):
    def test_canonical_checkout_long_url(self):
        self.assertEqual(
            "https://pay.openai.com/c/pay/cs_test_1",
            gen_link._canonical_checkout_long_url("cs_test_1"),
        )

    def test_canonical_checkout_long_url_is_empty_without_id(self):
        for blank in ("", "   ", None):
            with self.subTest(blank=blank):
                self.assertEqual("", gen_link._canonical_checkout_long_url(blank))

    def test_canonical_checkout_long_url_is_trimmed(self):
        self.assertEqual(
            "https://pay.openai.com/c/pay/cs_1",
            gen_link._canonical_checkout_long_url("  cs_1  "),
        )

    def test_hosted_checkout_url_is_rewritten_to_pay_openai(self):
        self.assertEqual(
            "https://pay.openai.com/c/pay/cs_1?x=1",
            gen_link._normalize_hosted_checkout_url("https://checkout.stripe.com/c/pay/cs_1?x=1"),
        )

    def test_unrelated_host_is_left_alone(self):
        url = "https://pay.openai.com/c/pay/cs_1"
        self.assertEqual(url, gen_link._normalize_hosted_checkout_url(url))

    def test_hosted_checkout_url_blank_input(self):
        for blank in ("", None, "   "):
            with self.subTest(blank=blank):
                self.assertEqual("", gen_link._normalize_hosted_checkout_url(blank))

    def test_chatgpt_checkout_url_needs_both_parts(self):
        self.assertEqual(
            "https://chatgpt.com/checkout/ent_1/cs_1",
            gen_link._chatgpt_checkout_url("ent_1", "cs_1"),
        )

    def test_chatgpt_checkout_url_is_empty_when_a_part_is_missing(self):
        self.assertEqual("", gen_link._chatgpt_checkout_url("", "cs_1"))
        self.assertEqual("", gen_link._chatgpt_checkout_url("ent_1", ""))
        self.assertEqual("", gen_link._chatgpt_checkout_url(None, None))

    def test_chatgpt_checkout_url_parts_are_trimmed(self):
        self.assertEqual(
            "https://chatgpt.com/checkout/ent/cs",
            gen_link._chatgpt_checkout_url(" ent ", " cs "),
        )

    def test_hosted_url_rewrite_feeds_the_canonical_fallback(self):
        """Stripe 给的 hosted URL 为空时，短链兜底会被用到。"""
        short = gen_link._canonical_checkout_long_url("cs_1")
        hosted = gen_link._normalize_hosted_checkout_url("") or short
        self.assertEqual(short, hosted)


# ────────────────────────────── country resolution ──────────────────────────────


class CheckoutCountryFromCfgTests(unittest.TestCase):
    def test_explicit_country_wins_over_everything(self):
        cfg = {"billing_regions": ["JP"], "checkout_country": "US", "target_country": "GB"}
        self.assertEqual("DE", gen_link._checkout_country_from_cfg(cfg, "de"))

    def test_first_billing_region_is_the_first_candidate(self):
        cfg = {"billing_regions": ["TH", "JP"], "checkout_country": "US"}
        self.assertEqual("TH", gen_link._checkout_country_from_cfg(cfg))

    def test_checkout_country_beats_billing_country(self):
        cfg = {"checkout_country": "US", "billing_country": "DE", "target_country": "GB"}
        self.assertEqual("US", gen_link._checkout_country_from_cfg(cfg))

    def test_billing_country_beats_target_country(self):
        self.assertEqual("DE", gen_link._checkout_country_from_cfg({"billing_country": "DE", "target_country": "GB"}))

    def test_target_country_is_used_before_the_default(self):
        self.assertEqual("GB", gen_link._checkout_country_from_cfg({"target_country": "GB"}))

    def test_default_is_used_when_config_is_empty(self):
        self.assertEqual("JP", gen_link._checkout_country_from_cfg({}))

    def test_default_is_overridable(self):
        self.assertEqual("SG", gen_link._checkout_country_from_cfg({}, default="SG"))

    def test_blank_candidates_are_skipped(self):
        """值为空串要继续往下找，不能直接返回空国家。"""
        cfg = {"billing_regions": [], "checkout_country": "  ", "billing_country": "", "target_country": "TH"}
        self.assertEqual("TH", gen_link._checkout_country_from_cfg(cfg))

    def test_non_list_billing_regions_is_ignored(self):
        cfg = {"billing_regions": "JP", "checkout_country": "US"}
        self.assertEqual("US", gen_link._checkout_country_from_cfg(cfg))

    def test_values_are_uppercased(self):
        self.assertEqual("JP", gen_link._checkout_country_from_cfg({"checkout_country": "jp"}))

    def test_explicit_country_is_uppercased_and_trimmed(self):
        self.assertEqual("JP", gen_link._checkout_country_from_cfg({"checkout_country": "us"}, " jp "))


# ────────────────────────────── token parsing ──────────────────────────────


class ParseTokenTests(unittest.TestCase):
    def test_three_part_jwt_is_accepted(self):
        self.assertEqual("a.b.c", gen_link.parse_token("a.b.c"))

    def test_whitespace_is_trimmed(self):
        self.assertEqual("a.b.c", gen_link.parse_token("  a.b.c\n"))

    def test_two_part_token_is_rejected(self):
        self.assertIsNone(gen_link.parse_token("a.b"))

    def test_four_part_string_is_rejected(self):
        self.assertIsNone(gen_link.parse_token("a.b.c.d"))

    def test_empty_segment_is_rejected(self):
        self.assertIsNone(gen_link.parse_token("a..c"))
        self.assertIsNone(gen_link.parse_token("a.b."))

    def test_blank_input_is_rejected(self):
        for blank in ("", "   ", None):
            with self.subTest(blank=blank):
                self.assertIsNone(gen_link.parse_token(blank))

    def test_token_without_dots_is_rejected(self):
        self.assertIsNone(gen_link.parse_token("opaque-token"))


# ────────────────────────────── config access ──────────────────────────────


class PayPalConfigTests(unittest.TestCase):
    def test_routes_through_payment_routing(self):
        cfg = {"paypal": {"target_country": "GB"}}
        with patch("sms_tool.payment_routing.method_payment_config",
                   return_value={"target_country": "JP"}) as routed:
            out = gen_link._paypal_config(cfg)
        routed.assert_called_once_with(cfg, "paypal")
        self.assertEqual({"target_country": "JP"}, out)

    def test_non_mapping_input_is_treated_as_empty(self):
        for bad in (None, [], "x", 42):
            with self.subTest(bad=bad):
                out = gen_link._paypal_config(bad)
                self.assertIsInstance(out, dict)

    def test_falls_back_to_legacy_paypal_block_on_import_error(self):
        cfg = {"paypal": {"target_country": "GB"}}
        with patch("sms_tool.payment_routing.method_payment_config",
                   side_effect=ImportError("no routing")):
            self.assertEqual({"target_country": "GB"}, gen_link._paypal_config(cfg))

    def test_legacy_fallback_ignores_non_mapping_paypal_block(self):
        with patch("sms_tool.payment_routing.method_payment_config",
                   side_effect=ImportError("no routing")):
            self.assertEqual({}, gen_link._paypal_config({"paypal": "nope"}))


class LoadJsonTests(unittest.TestCase):
    def _write(self, text: str, *, bom: bool = False) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "wb") as f:
            data = text.encode("utf-8")
            if bom:
                data = b"\xef\xbb\xbf" + data
            f.write(data)
        self.addCleanup(os.remove, path)
        return path

    def test_plain_object_is_loaded(self):
        path = self._write('{"paypal": {"target_country": "JP"}}')
        self.assertEqual({"paypal": {"target_country": "JP"}}, gen_link._load_json(path))

    def test_bom_is_tolerated(self):
        path = self._write('{"a": 1}', bom=True)
        self.assertEqual({"a": 1}, gen_link._load_json(path))

    def test_broken_json_returns_empty_dict(self):
        path = self._write("{not json")
        self.assertEqual({}, gen_link._load_json(path))

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual({}, gen_link._load_json(os.path.join(tempfile.gettempdir(), "definitely-absent.json")))

    def test_non_object_json_returns_empty_dict(self):
        """顶层是数组/标量时不能把 list 当成 config 往下传。"""
        self.assertEqual({}, gen_link._load_json(self._write("[1,2,3]")))
        self.assertEqual({}, gen_link._load_json(self._write('"scalar"')))

    def test_canonical_config_path_goes_through_the_shard_loader(self):
        """config.json 是分片配置，必须走 load_merged_config 而不是裸 json.load。"""
        with patch("sms_tool.config.load_merged_config", return_value={"merged": True}) as loader:
            out = gen_link._load_json(gen_link.DEFAULT_CONFIG_PATH)
        loader.assert_called_once_with()
        self.assertEqual({"merged": True}, out)

    def test_last_written_value_wins_for_a_temp_file(self):
        path = self._write(json.dumps({"a": 1}))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"a": 2}, f)
        self.assertEqual({"a": 2}, gen_link._load_json(path))


# ────────────────────────────── stage proxy preflight gate ──────────────────────────────


def _probe(**kw) -> ProxyProbeResult:
    defaults = dict(ok=True, stage="checkout", expected_country="JP",
                    ip="1.2.3.4", country_code="JP", country="Japan")
    defaults.update(kw)
    return ProxyProbeResult(**defaults)


class PrepareConfiguredStageProxyTests(unittest.TestCase):
    def _state(self) -> PayPalProxyState:
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        return PayPalProxyState(os.path.join(tmp, "state.json"))

    def test_empty_proxy_short_circuits_as_direct(self):
        """没配代理 → DIRECT，既不 probe 也不 rotate，也不 emit。

        emit 一并发出来断言：空代理时往日志里打一行
        "checkout proxy=DIRECT (preflight disabled)" 纯属噪音，短路分支就该
        什么都不说。这条同时保证「短路」是真的短路，而不是碰巧
        `redact_proxy_url("")` 也返回 "DIRECT" 造成的等价结果。
        """
        state = self._state()
        emitted = []
        with patch.object(gen_link, "probe_proxy") as probe, \
             patch.object(gen_link, "rotate_stage_proxy_session") as rotate:
            prepared, detail = gen_link._prepare_configured_stage_proxy(
                {}, state, "checkout", "", "JP",
                lambda step, msg, **kw: emitted.append((step, msg)))
        self.assertEqual("", prepared)
        self.assertEqual({"ok": True, "stage": "checkout", "proxy": "DIRECT"}, detail)
        probe.assert_not_called()
        rotate.assert_not_called()
        self.assertEqual([], emitted)

    def test_preflight_disabled_skips_the_probe(self):
        state = self._state()
        with patch.object(gen_link, "probe_proxy") as probe:
            prepared, detail = gen_link._prepare_configured_stage_proxy(
                {}, state, "checkout", "http://127.0.0.1:8080", "JP", gen_link._emit)
        self.assertEqual("http://127.0.0.1:8080", prepared)
        self.assertTrue(detail["ok"])
        self.assertEqual("checkout", detail["stage"])
        probe.assert_not_called()

    def test_preflight_enabled_runs_the_probe(self):
        state = self._state()
        with patch.object(gen_link, "probe_proxy", return_value=_probe()) as probe:
            prepared, detail = gen_link._prepare_configured_stage_proxy(
                {"preflight_proxy_check": True}, state, "checkout",
                "http://127.0.0.1:8080", "JP", gen_link._emit)
        probe.assert_called_once()
        self.assertEqual("http://127.0.0.1:8080", prepared)
        self.assertEqual("JP", detail["country_code"])
        self.assertIn("proxy", detail)

    def test_preflight_failure_raises_with_stage_and_expected_country(self):
        """预检失败是硬失败 —— 错误串里必须带上 stage/expected/actual 便于定位。"""
        state = self._state()
        with patch.object(gen_link, "probe_proxy",
                          return_value=_probe(ok=False, country_code="US", error="geo mismatch")):
            with self.assertRaises(RuntimeError) as ctx:
                gen_link._prepare_configured_stage_proxy(
                    {"preflight_proxy_check": True}, state, "approve",
                    "http://127.0.0.1:8080", "JP", gen_link._emit)
        msg = str(ctx.exception)
        self.assertIn("proxy_preflight_failed:approve", msg)
        self.assertIn("expected=JP", msg)
        self.assertIn("actual=US", msg)
        self.assertIn("geo mismatch", msg)

    def test_preflight_failure_without_expected_country_reports_any(self):
        state = self._state()
        with patch.object(gen_link, "probe_proxy",
                          return_value=_probe(ok=False, country_code="", error="timeout")):
            with self.assertRaises(RuntimeError) as ctx:
                gen_link._prepare_configured_stage_proxy(
                    {"preflight_proxy_check": True}, state, "approve",
                    "http://127.0.0.1:8080", "", gen_link._emit)
        self.assertIn("expected=ANY", str(ctx.exception))

    def test_preflight_failure_is_recorded_in_state(self):
        state = self._state()
        with patch.object(gen_link, "probe_proxy",
                          return_value=_probe(ok=False, country_code="US", error="geo mismatch")), \
             patch.object(state, "record_result") as record:
            with self.assertRaises(RuntimeError):
                gen_link._prepare_configured_stage_proxy(
                    {"preflight_proxy_check": True}, state, "approve",
                    "http://127.0.0.1:8080", "JP", gen_link._emit)
        record.assert_called_once_with("approve", "http://127.0.0.1:8080", False, "geo mismatch", "US")

    def test_rotation_is_applied_when_enabled(self):
        state = self._state()
        with patch.object(gen_link, "rotate_stage_proxy_session",
                          return_value="http://127.0.0.1:9999") as rotate:
            prepared, _ = gen_link._prepare_configured_stage_proxy(
                {"rotate_proxy_sessions": True}, state, "checkout",
                "http://127.0.0.1:8080", "JP", gen_link._emit)
        rotate.assert_called_once()
        self.assertEqual("http://127.0.0.1:9999", prepared)

    def test_rotation_is_skipped_when_disabled(self):
        state = self._state()
        with patch.object(gen_link, "rotate_stage_proxy_session") as rotate:
            gen_link._prepare_configured_stage_proxy(
                {}, state, "checkout", "http://127.0.0.1:8080", "JP", gen_link._emit)
        rotate.assert_not_called()

    def test_proxy_is_normalised_before_use(self):
        state = self._state()
        with patch.object(gen_link, "probe_proxy", return_value=_probe()):
            prepared, _ = gen_link._prepare_configured_stage_proxy(
                {"preflight_proxy_check": True}, state, "checkout",
                "127.0.0.1:8080", "JP", gen_link._emit)
        self.assertNotEqual("127.0.0.1:8080", prepared)
        self.assertTrue(str(prepared).startswith("http"), prepared)

    def test_expected_country_is_uppercased_for_the_probe(self):
        state = self._state()
        with patch.object(gen_link, "probe_proxy", return_value=_probe()) as probe:
            gen_link._prepare_configured_stage_proxy(
                {"preflight_proxy_check": True}, state, "checkout",
                "http://127.0.0.1:8080", "jp", gen_link._emit)
        self.assertEqual("JP", probe.call_args.kwargs["expected_country"])

    def test_probe_timeout_comes_from_config(self):
        state = self._state()
        with patch.object(gen_link, "probe_proxy", return_value=_probe()) as probe:
            gen_link._prepare_configured_stage_proxy(
                {"preflight_proxy_check": True, "proxy_probe_timeout_seconds": 3},
                state, "checkout", "http://127.0.0.1:8080", "JP", gen_link._emit)
        self.assertEqual(3.0, probe.call_args.kwargs["timeout"])

    def test_probe_timeout_defaults_to_twelve_seconds(self):
        state = self._state()
        with patch.object(gen_link, "probe_proxy", return_value=_probe()) as probe:
            gen_link._prepare_configured_stage_proxy(
                {"preflight_proxy_check": True}, state, "checkout",
                "http://127.0.0.1:8080", "JP", gen_link._emit)
        self.assertEqual(12.0, probe.call_args.kwargs["timeout"])

    def test_emit_is_called_with_a_redacted_proxy(self):
        """日志里不能出现代理口令 —— emit 的文案必须过 redact。"""
        state = self._state()
        emitted = []
        with patch.object(gen_link, "redact_proxy_url", return_value="REDACTED") as redact:
            gen_link._prepare_configured_stage_proxy(
                {}, state, "checkout", "http://user:pw@127.0.0.1:8080", "JP",
                lambda step, msg, **kw: emitted.append((step, msg)))
        redact.assert_called_once()
        # preflight 关闭分支发的就是 "proxy" 这一步，不带 stage 名。
        self.assertEqual([("proxy", "checkout proxy=REDACTED (preflight disabled)")], emitted)
        self.assertNotIn("pw", emitted[0][1])


if __name__ == "__main__":
    unittest.main()
