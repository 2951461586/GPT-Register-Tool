"""UPI 提链纯函数级回归测试。

本文件覆盖 ``sms_tool/upi_link.py`` 的**纯函数层**，全部离线，不碰网络、
不读真实 config、不写盘。

为什么要有这个文件
------------------
改造前 ``tests/`` 下**没有任何** ``test_upi*.py``，也没有一条用例引用
``_upi_extract_qr_from_html`` / ``_upi_merge_qr_key`` / ``_upi_scan_free_trial`` /
``_upi_hydrate_qr_data``。既有覆盖全在装配层
（``test_gen_pp_link.py::GeneratePpLinkContractTests``），只验「有没有把
请求发出去、参数对不对」，验不出解析逻辑本身错在哪。本项目实测过的两个缺陷
（base64url 缺补位、QR 类型裸子串判定）就是这样活下来的。

本文件把下列行为钉死为回归基线：
  1. base64url 解码必须容忍**缺省补位**（Stripe 在 URL fragment 里不给 ``=``）
  2. QR 图类型必须只看**路径**扩展名，不能被 query 里的 ``svg``/``png`` 干扰
  3. **静态资源过滤只按主机名**——一旦把 ``.png``/``.svg`` 后缀并进静态判据，
     QR 候选会被清空（本文件 ``test_qr_candidates_are_not_filtered_by_image_suffix``
     就是为这条钉的）
  4. approve 重试门禁必须严格区分「requires_approval」与「只是没有 redirect」
  5. custom 模式的 confirm/init 载荷必须带齐 custom 专有字段
"""

import base64
import json
import unittest

from sms_tool import upi_link


class Base64UrlDecodeTests(unittest.TestCase):
    """``_upi_decode_base64url_json`` 回归。

    旧实现是 ``b64decode(raw.replace("-","+").replace("_","/"))``——
    字母表替换其实是对的，真正缺的是 **``=`` 补位**。
    Stripe 放在 URL fragment 里的 base64url 通常不带 ``=``，
    于是 ``b64decode`` 抛 ``Incorrect padding`` 被 ``except`` 吞掉，
    整包 hosted instructions 数据静默丢弃。
    """

    def test_decodes_unpadded_payload(self):
        """核心回归：无补位的 base64url 必须能解出来。"""
        payload = {"url": "https://pay.openai.com/c/pay/cs_x?fid=abc>~"}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        unpadded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertNotIn("=", unpadded, "样本必须无补位，否则测不到点")
        self.assertEqual(upi_link._upi_decode_base64url_json(unpadded), payload)

    def test_decodes_padded_payload(self):
        payload = {"a": 1}
        padded = base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        ).decode()
        self.assertEqual(upi_link._upi_decode_base64url_json(padded), payload)

    def test_decodes_payload_containing_urlsafe_only_chars(self):
        """含 ``_`` / ``-`` 的样本（urlsafe 字母表专有字符）。"""
        payload = {"url": "https://pay.openai.com/c/pay/cs_x?fid=abc>~"}
        raw = json.dumps(payload, separators=(",", ":")).encode()
        encoded = base64.urlsafe_b64encode(raw).decode()
        self.assertTrue(
            "_" in encoded or "-" in encoded,
            "样本必须含 urlsafe 专有字符才测得到字母表分支",
        )
        self.assertEqual(upi_link._upi_decode_base64url_json(encoded), payload)

    def test_returns_none_on_invalid_input_without_raising(self):
        """非法输入一律返回 None，绝不抛异常（调用方靠 None 判断）。"""
        for bad in ("", "   ", None, "!!!not-base64!!!", "eyJhIjoxfQ--"):
            with self.subTest(bad=bad):
                self.assertIsNone(upi_link._upi_decode_base64url_json(bad))

    def test_trailing_dashes_are_not_silently_successful(self):
        """``eyJhIjoxfQ--`` 长度非法（10 字符），任何字母表都解不出——
        必须返回 None，不能假装成功。"""
        self.assertIsNone(upi_link._upi_decode_base64url_json("eyJhIjoxfQ--"))


class QrImageKindTests(unittest.TestCase):
    """``_upi_qr_image_kind`` / ``_upi_url_path_extension`` 回归。

    旧实现是 ``"png" if "png" in src.lower() else "svg"``：拿**整个 URL 的裸子串**
    判定。``.../image.png?format=svg&w=300`` 会因为 query 里的 ``svg`` 被判成
    svg，而路径里根本没有 svg；反过来任何不含 ``png`` 字样的 URL 都会被默认成 svg。
    """

    def test_extension_reads_path_only_not_query(self):
        """query 里的 ``svg`` 不能影响判定。"""
        url = "https://qr.stripe.com/abc/image.png?format=svg&w=300"
        self.assertEqual(upi_link._upi_url_path_extension(url), ".png")
        self.assertEqual(upi_link._upi_qr_image_kind(url), "png")

    def test_extension_reads_path_only_not_query_reverse(self):
        """反向：query 里的 ``png`` 不能把 svg 判成 png。"""
        url = "https://qr.stripe.com/abc/image.svg?v=2&x=png"
        self.assertEqual(upi_link._upi_url_path_extension(url), ".svg")
        self.assertEqual(upi_link._upi_qr_image_kind(url), "svg")

    def test_known_extensions(self):
        cases = {
            "https://qr.stripe.com/a/b.png": "png",
            "https://qr.stripe.com/a/b.PNG": "png",
            "https://qr.stripe.com/a/b.svg": "svg",
            "https://qr.stripe.com/a/b.jpg": "jpg",
            "https://qr.stripe.com/a/b.jpeg": "jpg",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(upi_link._upi_qr_image_kind(url), expected)

    def test_unknown_extension_returns_empty_not_svg(self):
        """无法判定时必须返回空串。

        旧实现这里会默认成 ``svg``，等于瞎猜一个通道登记进去。
        """
        for url in (
            "https://qr.stripe.com/abc/qr",
            "https://qr.stripe.com/abc/qr?type=png",
            "not a url at all",
            "",
        ):
            with self.subTest(url=url):
                self.assertEqual(upi_link._upi_qr_image_kind(url), "")

    def test_extension_of_extensionless_path_is_empty(self):
        self.assertEqual(
            upi_link._upi_url_path_extension("https://qr.stripe.com/abc/qr"), ""
        )


class StaticResourceTests(unittest.TestCase):
    """静态资源判据回归。

    🔴 这是改造过程中真实踩到的一个坑：把 ``.png`` / ``.svg`` 加进静态资源后缀后，
    ``_upi_extract_qr_candidates`` 的 ``is_qr_candidate and not is_static``
    会把**所有 QR 图**过滤掉——候选从 2 个变 0 个，QR 通道静默失效。
    参考实现的 ``is_known_static_host`` 只按主机名判定，本文件对齐这个语义。
    """

    def test_static_is_host_only(self):
        for host in (
            "https://js.stripe.com/v3/elements.js",
            "https://q.stripe.com/pixel",
            "https://files.stripe.com/logo.png",
            "https://m.stripe.network/inner.html",
            "https://stripe-camo.global.ssl.fastly.net/abc",
        ):
            with self.subTest(host=host):
                self.assertTrue(upi_link._upi_is_static_resource_url(host))

    def test_qr_host_is_not_static(self):
        """``qr.stripe.com`` 上的图片**不能**被判成静态资源。"""
        for url in (
            "https://qr.stripe.com/abc/q.png",
            "https://qr.stripe.com/abc/q.svg",
            "https://qr.stripe.com/abc/q.jpg",
        ):
            with self.subTest(url=url):
                self.assertFalse(upi_link._upi_is_static_resource_url(url))

    def test_data_image_is_not_static(self):
        """``data:image/...`` 是内联图，属于 QR 候选而不是静态资源。"""
        self.assertFalse(
            upi_link._upi_is_static_resource_url("data:image/png;base64,AAAA")
        )

    def test_code_resources_are_code_not_static(self):
        """js/css/字体走独立的 ``_upi_is_code_resource_url``。"""
        for url in (
            "https://example.com/app.js",
            "https://example.com/app.css",
            "https://example.com/f.woff2",
        ):
            with self.subTest(url=url):
                self.assertTrue(upi_link._upi_is_code_resource_url(url))
        self.assertFalse(
            upi_link._upi_is_code_resource_url("https://qr.stripe.com/a/q.png")
        )


class QrCandidateExtractionTests(unittest.TestCase):
    """``_upi_extract_qr_candidates`` / ``_upi_collect_urls`` 回归。"""

    def test_collects_nested_urls(self):
        payload = {
            "a": "https://qr.stripe.com/abc/q.png",
            "nested": {"b": ["https://qr.stripe.com/def/q.svg"]},
        }
        found = upi_link._upi_collect_urls(payload)
        self.assertIn("https://qr.stripe.com/abc/q.png", found)
        self.assertIn("https://qr.stripe.com/def/q.svg", found)

    def test_qr_candidates_are_not_filtered_by_image_suffix(self):
        """🔴 静态资源判据若并入图片后缀，这里会从 2 个变 0 个。"""
        payload = {
            "a": "https://qr.stripe.com/abc/q.png",
            "b": "https://qr.stripe.com/def/q.svg",
            "c": "https://stripe.com/img/logo.png",
        }
        candidates = upi_link._upi_extract_qr_candidates(payload)
        self.assertIn("https://qr.stripe.com/abc/q.png", candidates)
        self.assertIn("https://qr.stripe.com/def/q.svg", candidates)
        # 普通站点的 logo 既不是 QR 候选
        self.assertNotIn("https://stripe.com/img/logo.png", candidates)

    def test_candidates_are_deduplicated(self):
        payload = {
            "a": "https://qr.stripe.com/abc/q.png",
            "b": "https://qr.stripe.com/abc/q.png",
        }
        candidates = upi_link._upi_extract_qr_candidates(payload)
        self.assertEqual(candidates.count("https://qr.stripe.com/abc/q.png"), 1)

    def test_empty_payload(self):
        self.assertEqual(upi_link._upi_extract_qr_candidates({}), [])


class RedirectExtractionTests(unittest.TestCase):
    """``_upi_extract_redirect_url`` 回归。

    旧实现完全没有这一层，只知道 6 个标量 key，拿不到 ``redirect_to_url``
    这层嵌套，于是「明明有跳转却返回 hosted_url」。
    """

    def test_hosted_instructions_url(self):
        payload = {
            "next_action": {
                "hosted_instructions_url": "https://payments.stripe.com/upi/instructions/abc"
            }
        }
        self.assertEqual(
            upi_link._upi_extract_redirect_url(payload),
            "https://payments.stripe.com/upi/instructions/abc",
        )

    def test_redirect_to_url_nested(self):
        """旧实现漏掉的嵌套形态。"""
        payload = {
            "next_action": {"redirect_to_url": {"url": "https://pay.openai.com/c/pay/cs_y"}}
        }
        self.assertEqual(
            upi_link._upi_extract_redirect_url(payload),
            "https://pay.openai.com/c/pay/cs_y",
        )

    def test_returns_empty_when_no_redirect(self):
        self.assertEqual(upi_link._upi_extract_redirect_url({"status": "succeeded"}), "")

    def test_static_url_is_not_a_redirect(self):
        payload = {"next_action": {"hosted_instructions_url": "https://files.stripe.com/logo.png"}}
        self.assertEqual(upi_link._upi_extract_redirect_url(payload), "")

    def test_original_payload_is_not_mutated(self):
        payload = {"next_action": {"redirect_to_url": {"url": "https://pay.openai.com/c/pay/cs_y"}}}
        snapshot = json.dumps(payload, sort_keys=True)
        upi_link._upi_extract_redirect_url(payload)
        self.assertEqual(json.dumps(payload, sort_keys=True), snapshot)


class SubmissionAttemptTests(unittest.TestCase):
    """``_upi_find_submission_attempt`` 回归。"""

    def test_requires_approval(self):
        payload = {"submission_attempt": {"state": "requires_approval", "id": "sa_1"}}
        attempt = upi_link._upi_find_submission_attempt(payload)
        self.assertEqual(attempt.get("state"), "requires_approval")

    def test_failed(self):
        payload = {"submission_attempt": {"state": "failed", "id": "sa_2"}}
        attempt = upi_link._upi_find_submission_attempt(payload)
        self.assertEqual(attempt.get("state"), "failed")

    def test_absent_returns_empty_dict(self):
        self.assertEqual(upi_link._upi_find_submission_attempt({"status": "succeeded"}), {})

    def test_non_dict_payload_returns_empty_dict(self):
        for bad in (None, "x", 3, []):
            with self.subTest(bad=bad):
                self.assertEqual(upi_link._upi_find_submission_attempt(bad), {})


class SetupIntentBlockedTests(unittest.TestCase):
    """``_upi_setup_intent_last_error`` / ``_upi_raise_if_setup_intent_blocked`` 回归。

    ``generic_decline`` 是 provider 侧风控拒绝（终局），旧实现把它当成
    「还在等」继续轮询，白等到超时。这里钉住它能被正确识别并抛出。
    """

    def test_last_error_extracted(self):
        payload = {"setup_intent": {"last_setup_error": {"code": "generic_decline"}}}
        self.assertIn("generic_decline", upi_link._upi_setup_intent_last_error(payload))

    def test_last_error_empty_when_absent(self):
        payload = {"setup_intent": {"status": "requires_action"}}
        self.assertEqual(upi_link._upi_setup_intent_last_error(payload), "")

    def test_decline_text_detection(self):
        self.assertTrue(upi_link._upi_is_provider_decline_text("generic_decline"))
        self.assertFalse(upi_link._upi_is_provider_decline_text("requires_action"))

    def test_raises_on_generic_decline(self):
        payload = {"setup_intent": {"last_setup_error": {"code": "generic_decline"}}}
        with self.assertRaises(Exception):
            upi_link._upi_raise_if_setup_intent_blocked(payload, "unit test")

    def test_does_not_raise_on_healthy_payload(self):
        payload = {"status": "succeeded", "setup_intent": {"status": "requires_action"}}
        upi_link._upi_raise_if_setup_intent_blocked(payload, "unit test")


class FreeTrialTests(unittest.TestCase):
    """``_upi_get_free_trial_status`` / ``_upi_scan_free_trial`` 回归。"""

    def test_zero_due_is_free_trial(self):
        status = upi_link._upi_get_free_trial_status(
            {
                "total_summary": {"due": 0, "currency": "inr"},
                "payment_method_types": ["card", "upi"],
            }
        )
        self.assertTrue(status["has_free_trial"])
        self.assertTrue(status["has_upi"])
        self.assertEqual(status["due"], 0)

    def test_nonzero_due_is_not_free_trial(self):
        status = upi_link._upi_get_free_trial_status(
            {"total_summary": {"due": 49900, "currency": "inr"}}
        )
        self.assertFalse(status["has_free_trial"])

    def test_scan_handles_non_dict(self):
        """``_upi_scan_free_trial`` 必须能吞下任意类型而不抛。"""
        for value in (None, "x", 1, [], {}):
            with self.subTest(value=value):
                self.assertIsInstance(upi_link._upi_scan_free_trial(value), dict)


class AmountTests(unittest.TestCase):
    """``_upi_amount_minor`` / ``_upi_nested_get`` 回归。"""

    def test_amount_minor(self):
        self.assertEqual(upi_link._upi_amount_minor(0), 0)
        self.assertEqual(upi_link._upi_amount_minor(49900), 49900)
        self.assertEqual(upi_link._upi_amount_minor(499.0), 499)
        self.assertIsNone(upi_link._upi_amount_minor(None))
        # 字符串不猜语义, 返回 None
        self.assertIsNone(upi_link._upi_amount_minor("49900"))

    def test_nested_get(self):
        self.assertEqual(
            upi_link._upi_nested_get({"a": {"b": {"c": 7}}}, ["a", "b", "c"]), 7
        )
        self.assertIsNone(upi_link._upi_nested_get({"a": {}}, ["a", "b", "c"]))
        self.assertIsNone(upi_link._upi_nested_get(None, ["a"]))


class MergeQrKeyTests(unittest.TestCase):
    """``_upi_merge_qr_key`` 回归：合并语义与「不覆盖已有值」。"""

    def test_upi_uri_sets_two_keys(self):
        result = {}
        upi_link._upi_merge_qr_key(result, "some_key", "upi://pay?pa=x@y&am=0")
        self.assertEqual(result["upi_uri"], "upi://pay?pa=x@y&am=0")
        self.assertEqual(result["mobile_auth_url"], "upi://pay?pa=x@y&am=0")

    def test_does_not_overwrite_existing_upi_uri(self):
        result = {"upi_uri": "upi://first"}
        upi_link._upi_merge_qr_key(result, "k", "upi://second")
        self.assertEqual(result["upi_uri"], "upi://first")

    def test_instructions_url_routed(self):
        result = {}
        url = "https://payments.stripe.com/upi/instructions/abc"
        upi_link._upi_merge_qr_key(result, "k", url)
        self.assertEqual(result["hosted_instructions_url"], url)

    def test_qr_png_and_svg_routed_by_extension(self):
        result = {}
        upi_link._upi_merge_qr_key(result, "k", "https://qr.stripe.com/a/q.png")
        upi_link._upi_merge_qr_key(result, "k2", "https://qr.stripe.com/a/q.svg")
        self.assertEqual(result["qr_image_url_png"], "https://qr.stripe.com/a/q.png")
        self.assertEqual(result["qr_image_url_svg"], "https://qr.stripe.com/a/q.svg")

    def test_none_value_is_ignored(self):
        result = {}
        upi_link._upi_merge_qr_key(result, "k", None)
        self.assertEqual(result, {})


class ConfirmBodyTests(unittest.TestCase):
    """``_upi_build_confirm_body`` 回归：custom 模式的专有字段必须带齐。

    这些字段是 ``checkout_ui_mode=custom`` 的协议后果——``hosted`` 下不需要，
    custom 下缺了就 confirm 不通过。旧实现用 hosted，所以一个都没有。
    """

    def _body(self, **overrides):
        kwargs = dict(
            cs_id="cs_live_1",
            stripe_pk="pk_test_1",
            ctx={
                "guid": "g" * 16,
                "muid": "m" * 16,
                "sid": "s" * 16,
                "client_session_id": "csi_1",
                "elements_session_id": "es_1",
                "elements_session_config_id": "esc_1",
                "config_id": "cfg_1",
                "init_checksum": "chk_1",
                "stripe_version": "2025-03-31.basil",
            },
            processor_entity="openai_ie",
            init_payload={"total_summary": {"due": 0, "currency": "inr"}},
            billing={"name": "N", "email": "e@x.com", "country": "IN"},
            fingerprint={"user_agent": "UA", "locale": "en", "timezone": "Asia/Kolkata"},
            pm_id="pm_1",
            inline_pm=True,
            return_url="https://chatgpt.com/",
        )
        kwargs.update(overrides)
        return upi_link._upi_build_confirm_body(**kwargs)

    def test_custom_only_fields_present(self):
        """custom 专有字段。字段清单与参考实现逐字对齐——
        参考实现只有 5 个 ``last_displayed_line_item_group_details[*]``，
        没有 ``total_tax_amount``（那是 exlusive/inclusive 两项之和，不单独传）。"""
        body = self._body()
        for key in (
            "expected_amount",
            "expected_payment_method_type",
            "last_displayed_line_item_group_details[subtotal]",
            "last_displayed_line_item_group_details[total_exclusive_tax]",
            "last_displayed_line_item_group_details[total_inclusive_tax]",
            "last_displayed_line_item_group_details[total_discount_amount]",
            "last_displayed_line_item_group_details[shipping_rate_amount]",
            "consent[terms_of_service]",
            "link_brand",
        ):
            with self.subTest(key=key):
                self.assertIn(key, body)

    def test_expected_amount_matches_init_total_due(self):
        """``expected_amount`` 必须来自 init 的 total_summary.due（0 元试用 ⇒ "0"）。"""
        self.assertEqual(self._body().get("expected_amount"), "0")

    def test_browser_identity_triplet_present(self):
        """guid / muid / sid 是 Stripe.js 的身份三元组，custom 下必须有。"""
        body = self._body()
        self.assertEqual(body.get("guid"), "g" * 16)
        self.assertEqual(body.get("muid"), "m" * 16)
        self.assertEqual(body.get("sid"), "s" * 16)

    def test_terms_of_service_accepted(self):
        self.assertEqual(self._body().get("consent[terms_of_service]"), "accepted")

    def test_inline_pm_embeds_payment_method_data(self):
        body = self._body()
        self.assertIn("payment_method_data[type]", body)
        self.assertEqual(body["payment_method_data[type]"], "upi")

    def test_no_inline_pm_omits_payment_method_data(self):
        body = self._body(inline_pm=False)
        self.assertNotIn("payment_method_data[type]", body)

    def test_client_attribution_metadata_carries_session_ids(self):
        """四个 session id 走 ``client_attribution_metadata[*]`` 前缀键。

        注意键名是带前缀的扁平形式，不是顶层 ``client_session_id``。
        """
        body = self._body()
        self.assertEqual(
            body.get("client_attribution_metadata[client_session_id]"), "csi_1"
        )
        self.assertEqual(
            body.get("client_attribution_metadata[elements_session_id]"), "es_1"
        )
        self.assertEqual(
            body.get("client_attribution_metadata[elements_session_config_id]"), "esc_1"
        )
        self.assertEqual(
            body.get("client_attribution_metadata[checkout_config_id]"), "cfg_1"
        )
        self.assertEqual(
            body.get("client_attribution_metadata[checkout_session_id]"), "cs_live_1"
        )

    def test_attribution_metadata_uses_custom_checkout_flow(self):
        """custom 模式的归因四元组：checkout / payment-element / custom_checkout / deferred。"""
        body = self._body()
        self.assertEqual(
            body.get("client_attribution_metadata[merchant_integration_source]"), "checkout"
        )
        self.assertEqual(
            body.get("client_attribution_metadata[merchant_integration_subtype]"),
            "payment-element",
        )
        self.assertEqual(
            body.get("client_attribution_metadata[merchant_integration_version]"),
            "custom_checkout",
        )
        self.assertEqual(
            body.get("client_attribution_metadata[payment_intent_creation_flow]"), "deferred"
        )

    def test_all_values_are_strings(self):
        """Stripe form-encoded 请求体必须是 str -> str。"""
        for key, value in self._body().items():
            with self.subTest(key=key):
                self.assertIsInstance(value, str)


class InitBodyTests(unittest.TestCase):
    """``_upi_build_init_body`` 回归。"""

    def test_custom_checkout_betas(self):
        body = upi_link._upi_build_init_body(
            "pk_test_1",
            {"locale": "en", "timezone": "Asia/Kolkata", "user_agent": "UA",
             "accept_language": "en-IN,en;q=0.9"},
            "js_1",
        )
        betas = {v for k, v in body.items() if "client_betas" in k}
        self.assertIn("custom_checkout_server_updates_1", betas)
        self.assertIn("custom_checkout_manual_approval_1", betas)
        self.assertEqual(body.get("elements_session_client[elements_init_source]"), "custom_checkout")

    def test_all_values_are_strings(self):
        body = upi_link._upi_build_init_body(
            "pk_test_1", {"locale": "en", "timezone": "Asia/Kolkata"}, "js_1"
        )
        for key, value in body.items():
            with self.subTest(key=key):
                self.assertIsInstance(value, str)


class ApproveGatingTests(unittest.TestCase):
    """approve 重试退避 ``_approve_backoff`` 回归。

    旧实现的 approve 循环**完全不睡**，60 次请求瞬间打完（对端只看得到一串
    并发），改造后加了确定性退避。同时把 cap 做成可调，测试才能把它置 0。
    """

    def test_cap_zero_means_no_sleep(self):
        for attempt in (1, 2, 30, 99):
            with self.subTest(attempt=attempt):
                self.assertEqual(upi_link._approve_backoff(attempt, 0), 0.0)

    def test_linear_growth_then_capped(self):
        self.assertEqual(upi_link._approve_backoff(1, 1.5), 0.5)
        self.assertEqual(upi_link._approve_backoff(3, 1.5), 1.5)
        self.assertEqual(upi_link._approve_backoff(4, 1.5), 1.5)
        self.assertEqual(upi_link._approve_backoff(99, 1.5), 1.5)

    def test_never_negative(self):
        for attempt in (0, -1, 1, 60):
            with self.subTest(attempt=attempt):
                self.assertGreaterEqual(upi_link._approve_backoff(attempt, 1.5), 0.0)


class SecondConfirmGateTests(unittest.TestCase):
    """``_upi_should_retry_second_confirm`` 回归。

    只有「超时/没找到」才值得再 confirm 一次；provider 明确拒绝
    （generic_decline）必须直接放弃，不能靠再 confirm 绕过去。
    """

    def test_timeout_triggers_retry(self):
        self.assertTrue(
            upi_link._upi_should_retry_second_confirm(
                RuntimeError("redirect url resolution timeout: waiting")
            )
        )

    def test_provider_decline_does_not_retry(self):
        self.assertFalse(
            upi_link._upi_should_retry_second_confirm(
                RuntimeError("generic_decline provider declined")
            )
        )


class FingerprintSelfConsistencyTests(unittest.TestCase):
    """指纹模板必须**自洽**：UA 与 sec-ch-ua-platform 不能互相矛盾。

    旧实现给每个 session 各随机一个 UA，于是出现「UA 说 macOS、
    sec-ch-ua-platform 说 Windows」这种一眼假的组合。
    """

    def test_all_templates_are_self_consistent(self):
        for index, template in enumerate(upi_link.UPI_FINGERPRINT_TEMPLATES):
            with self.subTest(index=index, name=template.get("name")):
                ua = template["user_agent"]
                platform = template["sec_ch_ua_platform"]
                if "Windows NT" in ua:
                    self.assertEqual(platform, '"Windows"')
                elif "Macintosh" in ua:
                    self.assertEqual(platform, '"macOS"')
                elif "Linux" in ua:
                    self.assertEqual(platform, '"Linux"')
                else:
                    self.fail("UA 平台无法识别: %s" % ua)

    def test_all_templates_have_required_keys(self):
        required = {
            "name", "impersonate", "user_agent", "sec_ch_ua",
            "sec_ch_ua_mobile", "sec_ch_ua_platform",
            "locale", "elements_locale", "timezone", "accept_language",
        }
        for template in upi_link.UPI_FINGERPRINT_TEMPLATES:
            with self.subTest(name=template.get("name")):
                self.assertTrue(required.issubset(template.keys()))

    def test_index_is_deterministic(self):
        self.assertEqual(upi_link._upi_fingerprint(0), upi_link._upi_fingerprint(0))

    def test_index_wraps_around(self):
        count = len(upi_link.UPI_FINGERPRINT_TEMPLATES)
        self.assertEqual(
            upi_link._upi_fingerprint(count), upi_link._upi_fingerprint(0)
        )

    def test_apply_fingerprint_sets_headers(self):
        class FakeSession:
            def __init__(self):
                self.headers = {}

        session = FakeSession()
        upi_link._upi_apply_fingerprint(session, upi_link._upi_fingerprint(0))
        self.assertIn("User-Agent", session.headers)
        self.assertIn("sec-ch-ua", session.headers)
        self.assertIn("sec-ch-ua-platform", session.headers)
        self.assertIn("Accept-Language", session.headers)

    def test_apply_fingerprint_tolerates_bad_session(self):
        """session 为 None 或 headers 不可写时不能抛。"""
        upi_link._upi_apply_fingerprint(None, upi_link._upi_fingerprint(0))
        upi_link._upi_apply_fingerprint(object(), upi_link._upi_fingerprint(0))


class BillingProfileTests(unittest.TestCase):
    """``_upi_billing_profile`` 回归。"""

    def test_default_is_india(self):
        profile = upi_link._upi_billing_profile()
        self.assertEqual(profile["country"], "IN")
        for key in ("name", "email", "line1", "city", "state", "postal_code"):
            with self.subTest(key=key):
                self.assertTrue(profile.get(key))

    def test_fixed_billing_overrides(self):
        profile = upi_link._upi_billing_profile(
            {"fixed_billing": {"country": "IN", "city": "Mumbai"}}
        )
        self.assertEqual(profile["city"], "Mumbai")

    def test_country_is_upper_cased(self):
        profile = upi_link._upi_billing_profile({"fixed_billing": {"country": "in"}})
        self.assertEqual(profile["country"], "IN")


class NextActionTests(unittest.TestCase):
    """``_upi_extract_next_action`` 兼容层回归。

    这个函数是公开面的一部分（``paypal_link`` 再导出），即使内部实现换了，
    行为契约不能变。
    """

    def test_extracts_upi_uri(self):
        payload = {"next_action": {"upi_uri": "upi://pay?pa=x@y"}}
        self.assertEqual(
            upi_link._upi_extract_next_action(payload).get("upi_uri"),
            "upi://pay?pa=x@y",
        )

    def test_returns_dict_for_arbitrary_input(self):
        for payload in (None, "x", 1, [], {}):
            with self.subTest(payload=payload):
                self.assertIsInstance(upi_link._upi_extract_next_action(payload), dict)


class HtmlQrExtractionTests(unittest.TestCase):
    """``_upi_extract_qr_from_html`` 回归。

    旧实现在 ``<img>`` 循环里命中**首个**就 ``break``，一旦首个是占位图
    就直接丢结果。
    """

    def test_finds_qr_image(self):
        html = '<html><body><img src="https://qr.stripe.com/abc/q.png"></body></html>'
        found = upi_link._upi_extract_qr_from_html(html)
        self.assertTrue(found, "应当至少提取到一个字段")

    def test_does_not_stop_at_first_non_qr_image(self):
        """首个 img 是素材图时，不能因此丢掉后面的真实 QR 图。"""
        html = (
            '<html><body>'
            '<img src="https://stripe.com/img/logo.png">'
            '<img src="https://qr.stripe.com/abc/real.png">'
            '</body></html>'
        )
        found = upi_link._upi_extract_qr_from_html(html)
        joined = json.dumps(found)
        self.assertIn("qr.stripe.com/abc/real.png", joined)

    def test_empty_html(self):
        self.assertIsInstance(upi_link._upi_extract_qr_from_html(""), dict)


class PublicSurfaceTests(unittest.TestCase):
    """公开符号面回归。

    ``sms_tool/paypal_link/__init__.py`` 与 ``gen_link.py`` 会把这些名字
    再导出给外部调用方，删任何一个都会破坏兼容壳。
    """

    REQUIRED = (
        "UPI_CHECKOUT_URL",
        "UPI_CHECKOUT_CONFIRM_URL",
        "UPI_CHECKOUT_APPROVE_URL",
        "STRIPE_PAYMENT_PAGE_INIT_URL_T",
        "STRIPE_PAYMENT_PAGE_CONFIRM_URL_T",
        "STRIPE_PAYMENT_PAGE_GET_URL_T",
        "UPI_APPROVAL_MAX_ATTEMPTS",
        "UPI_QR_POLL_MAX_ATTEMPTS",
        "UPI_QR_POLL_INTERVAL",
        "UPI_BILLING_IN",
        "_default_qr_path",
        "_write_qr_png",
        "_upi_nested_get",
        "_upi_amount_minor",
        "_upi_extract_payment_amount",
        "_upi_get_payment_method_types",
        "_upi_scan_free_trial",
        "_upi_get_free_trial_status",
        "_upi_merge_qr_key",
        "_upi_extract_next_action",
        "_upi_extract_qr_from_html",
        "_upi_hydrate_qr_data",
        "_method_cfg",
        "_payment_stage_proxies_from_config",
        "generate_upi_qr_link",
    )

    def test_required_symbols_exist(self):
        missing = [name for name in self.REQUIRED if not hasattr(upi_link, name)]
        self.assertEqual(missing, [], "公开符号面被破坏，缺失: %s" % missing)

    def test_public_constants_unchanged(self):
        """常量值是 compat 契约的一部分，改动要单独评估。"""
        self.assertEqual(upi_link.UPI_APPROVAL_MAX_ATTEMPTS, 60)
        self.assertEqual(upi_link.UPI_QR_POLL_MAX_ATTEMPTS, 30)
        self.assertEqual(upi_link.UPI_QR_POLL_INTERVAL, 1.0)

    def test_billing_in_keys_unchanged(self):
        expected = {
            "name", "email", "line1", "line2", "city", "state", "postal", "country",
        }
        self.assertEqual(set(upi_link.UPI_BILLING_IN), expected)


class FailureClassificationTests(unittest.TestCase):
    """``_upi_classify_failure`` 回归。

    历史实现所有异常一律返回 ``"upi_qr_failed"``，调用方只能对 ``error``
    字符串做子串匹配；而 ``sms_tool.error_classification`` 的注册表里
    没有 ``generic_decline`` / ``approve blocked`` / ``checkout_not_active_session``
    这些 UPI 专有说法（实测都归到 ``unknown``）。这里钉住细分代码。
    """

    def test_checkout_not_active(self):
        self.assertEqual(
            upi_link._upi_classify_failure("checkout_not_active_session"),
            "upi_checkout_not_active",
        )

    def test_provider_declined(self):
        for text in ("generic_decline", "generic_decline provider declined", "approve blocked"):
            with self.subTest(text=text):
                self.assertEqual(
                    upi_link._upi_classify_failure(text), "upi_provider_declined"
                )

    def test_redirect_timeout(self):
        self.assertEqual(
            upi_link._upi_classify_failure("redirect url resolution timeout: waiting"),
            "upi_redirect_timeout",
        )

    def test_unauthorized(self):
        self.assertEqual(
            upi_link._upi_classify_failure("access_token invalid or expired (401)"),
            "upi_checkout_unauthorized",
        )

    def test_unknown_falls_back(self):
        for text in ("poll transport error: AttributeError", "something else", "", None):
            with self.subTest(text=text):
                self.assertEqual(upi_link._upi_classify_failure(text), "upi_qr_failed")

    def test_never_returns_empty(self):
        """无论输入什么，都必须给出一个非空代码（调用方当键用）。"""
        for text in (None, "", 0, [], object()):
            with self.subTest(text=repr(text)):
                self.assertTrue(upi_link._upi_classify_failure(text))

    def test_classification_is_idempotent(self):
        """🔴 对自己产出的 code 再分类一次，必须得到同一个 code。

        为什么这条必须存在：``error_code`` 会被存进 session / progress，
        上层复盘时常常拿这个 code **再喂一次**分类器。若幂等不成立，
        ``upi_redirect_timeout`` 会被按文本匹配规则判成 ``upi_qr_failed``
        ——同一个串自己分类自己得到不同结果，这类不一致最难查。

        变异判据：把 ``if text.strip() == known:`` 改成 ``if False:``，本条变红。
        """
        for code in (
            "upi_checkout_not_active",
            "upi_provider_declined",
            "upi_redirect_timeout",
            "upi_checkout_unauthorized",
            "upi_qr_failed",
        ):
            with self.subTest(code=code):
                once = upi_link._upi_classify_failure(code)
                twice = upi_link._upi_classify_failure(once)
                self.assertEqual(
                    once, code,
                    "自身 code 未被识别: %r -> %r" % (code, once),
                )
                self.assertEqual(
                    once, twice,
                    "不幂等: %r -> %r -> %r" % (code, once, twice),
                )

    def test_registry_agrees_with_upi_classifier(self):
        """两套分类器必须同向：upi code 在注册表里要归 ``upi_payment``。

        历史问题：这些 UPI 专有说法在 ``failure_registry`` 里**一个都没有**，
        全部落 ``unknown``；而 ``unknown`` 在重试守卫眼里等同终态，会让
        「真终态」与「可重试」拿到同样处置。
        """
        from sms_tool.error_classification import classify_error

        for code in (
            "upi_checkout_not_active",
            "upi_provider_declined",
            "upi_redirect_timeout",
            "upi_checkout_unauthorized",
            "upi_qr_failed",
        ):
            with self.subTest(code=code):
                self.assertEqual(classify_error(code), "upi_payment")


class UiModeConfigTests(unittest.TestCase):
    """``checkout_ui_mode`` 默认值回归。

    协议方向必须是 ``custom``。历史坑：``upi_link.py`` 里默认 ``custom``，
    但 ``config.json`` / ``payment.json`` 的 ``upi`` 段显式写了 ``hosted``，
    配置段优先级更高 ⇒ 代码改了也白改，运行期仍是 hosted。
    这里同时钉住「配置段确实存在且不是 hosted」。
    """

    def test_ui_mode_resolution_accepts_custom(self):
        """``checkout_ui_mode`` 的解析只接受 custom / hosted，其它值回落到 custom。

        不去断言源码里有哪个字符串字面量——那是实现细节，重构一挪位置就假红
        （实测踩过：把字面量提进 helper 后，基于 ``inspect.getsource`` 的断言挂了）。
        这里改断言**行为契约**：``_upi_build_init_body`` 产出的就是 custom
        协议的特征载荷，它存在即说明 custom 是被支持的一等公民。
        """
        body = upi_link._upi_build_init_body(
            "pk_test_1", {"locale": "en", "timezone": "Asia/Kolkata"}, "js_1"
        )
        betas = {v for k, v in body.items() if "client_betas" in k}
        self.assertIn("custom_checkout_server_updates_1", betas)
        self.assertEqual(
            body.get("elements_session_client[elements_init_source]"), "custom_checkout"
        )

    def test_ui_mode_value_set_is_enumerated(self):
        """custom 与 hosted 都是合法取值——hosted 只是不再是默认。"""
        import inspect

        source = inspect.getsource(upi_link)
        self.assertIn('"custom"', source)
        self.assertIn('"hosted"', source)

    def test_shipped_configs_use_custom_for_upi(self):
        """仓库里随附的配置必须与代码默认值同向，否则协议切换形同虚设。"""
        from pathlib import Path

        root = Path(upi_link.__file__).resolve().parents[1]
        for name in ("config.json", "config.example.json", "payment.json"):
            path = root / name
            if not path.exists():
                continue
            with self.subTest(config=name):
                data = json.loads(path.read_text(encoding="utf-8"))
                upi_cfg = data.get("upi") or {}
                self.assertEqual(
                    upi_cfg.get("checkout_ui_mode"),
                    "custom",
                    "%s 的 upi.checkout_ui_mode 不是 custom，会覆盖代码默认值" % name,
                )


class DumpChannelTests(unittest.TestCase):
    """``_upi_dump_http`` 诊断通道回归（P3-1）。

    这个通道的价值是「失败现场留证据」，所以三条不变量必须钉死：

    1. **默认不写盘** —— 正常跑批不该在仓库里堆文件
    2. **4xx/5xx 强制写盘** —— 调用方对失败响应传 ``force=True``，
       哪怕 ``UPI_DUMP`` 没开也要留证（这条最容易在重构里被抹掉：
       只要有人把 ``force`` 的短路判断写反或删掉，失败现场就没了）
    3. **脱敏必须生效** —— dump 里出现明文 token/代理凭据 = 事故

    测试全程用 ``tempfile.TemporaryDirectory`` + ``patch.dict(os.environ)``，
    绝不碰真实的 ``runtime/upi_dumps``。
    """

    class _FakeResponse:
        def __init__(self, text, status_code=200, url="https://example.test/x"):
            self.text = text
            self.status_code = status_code
            self.url = url

    def _drain(self, tmpdir):
        """返回 tmpdir 下所有 dump 文件路径（按名排序）。"""
        import os

        found = []
        for dirpath, _dirnames, filenames in os.walk(tmpdir):
            for name in filenames:
                found.append(os.path.join(dirpath, name))
        return sorted(found)

    def test_dump_is_off_by_default(self):
        """默认（UPI_DUMP 未设、force=False）绝不写盘。"""
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            env = {"UPI_DUMP_DIR": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("UPI_DUMP", None)
                written = upi_link._upi_dump_http(self._FakeResponse("hello"), "stage_a")
            self.assertEqual(written, "", "默认状态不应返回落盘路径")
            self.assertEqual(self._drain(tmp), [], "默认状态不应产生任何 dump 文件")

    def test_dump_writes_when_switch_on(self):
        """``UPI_DUMP=1`` 时正常落盘，且内容含 stage / status / body。"""
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"UPI_DUMP": "1", "UPI_DUMP_DIR": tmp}, clear=False):
                written = upi_link._upi_dump_http(
                    self._FakeResponse("resp-body-xyz", status_code=201),
                    "stage_b", {"k": "v"}, "POST", "https://example.test/post",
                )
            self.assertTrue(written, "开关打开时应返回落盘路径")
            self.assertTrue(os.path.exists(written))
            with open(written, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("stage: stage_b", content)
            self.assertIn("status: 201", content)
            self.assertIn("resp-body-xyz", content)
            self.assertIn('"k": "v"', content)
            self.assertEqual(self._drain(tmp), [written], "应且只应产生一个文件")

    def test_force_writes_even_when_switch_off(self):
        """🔴 失败响应必须强制留证：``force=True`` 不依赖 ``UPI_DUMP``。

        变异判据：删掉 ``if not force and not _env_bool(...)`` 里的 ``force``
        短路，本条立刻变红。
        """
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            env = {"UPI_DUMP_DIR": tmp}
            with mock.patch.dict(os.environ, env, clear=False):
                os.environ.pop("UPI_DUMP", None)
                written = upi_link._upi_dump_http(
                    self._FakeResponse('{"error":"boom"}', status_code=402),
                    "stage_forbidden", None, "POST", "https://example.test/forbidden",
                    force=True,
                )
            self.assertTrue(written, "force=True 时即便未开总开关也必须落盘")
            with open(written, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertIn("status: 402", content)
            self.assertIn("boom", content)

    def test_redaction_strips_all_four_credential_classes(self):
        """Bearer / session-token / token 字段 / 内联代理凭据 四类必须全被抹掉。

        变异判据：把 ``_upi_redact_for_dump`` 里任一条 ``re.sub`` 删掉，
        对应子用例变红。
        """
        cases = [
            (
                "bearer",
                "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
                ["eyJhbGciOiJIUzI1NiJ9.payload.sig"],
            ),
            (
                "session_token",
                "__Secure-next-auth.session-token=supersecretvalue123; path=/",
                ["supersecretvalue123"],
            ),
            (
                "token_field",
                '{"access_token": "sk-abcdef123456", "token": "zzzzzzzzzzzz"}',
                ["sk-abcdef123456", "zzzzzzzzzzzz"],
            ),
            (
                "proxy_credentials",
                "proxy=http://lizi1_custom_zone_US:451203zhy@us.ipwo.net:7878",
                ["451203zhy"],
            ),
        ]
        for label, raw, secrets in cases:
            with self.subTest(credential_class=label):
                redacted = upi_link._upi_redact_for_dump(raw)
                for secret in secrets:
                    self.assertNotIn(
                        secret, redacted,
                        "%s 类凭据未被脱敏：%r" % (label, redacted),
                    )
                self.assertIn("***", redacted, "%s 类应留下脱敏占位符" % label)

    def test_redaction_applies_to_written_file(self):
        """落盘文件本身必须是脱敏后的——脱敏函数写对了但没接进落盘等于零。

        变异判据：把 ``_upi_dump_http`` 里两处 ``_upi_redact_for_dump(...)``
        调用去掉，本条变红。
        """
        import os
        import tempfile
        from unittest import mock

        secret = "sk-live-must-not-leak-0001"
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"UPI_DUMP": "1", "UPI_DUMP_DIR": tmp}, clear=False):
                written = upi_link._upi_dump_http(
                    self._FakeResponse('{"access_token": "%s"}' % secret),
                    "stage_secret",
                    {"Authorization": "Bearer %s" % secret},
                    "POST", "https://example.test/secret",
                )
            with open(written, "r", encoding="utf-8") as fh:
                content = fh.read()
            self.assertNotIn(secret, content, "落盘文件里出现了明文凭据")
            self.assertIn("***", content)

    def test_dump_limit_truncates(self):
        """``UPI_DUMP_LIMIT`` 截断响应体，防止超大 HTML 把磁盘写满。"""
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ, {"UPI_DUMP": "1", "UPI_DUMP_DIR": tmp, "UPI_DUMP_LIMIT": "500"},
                clear=False,
            ):
                written = upi_link._upi_dump_http(
                    self._FakeResponse("A" * 4000), "stage_big",
                )
            with open(written, "r", encoding="utf-8") as fh:
                content = fh.read()
            # limit=500 是下限本身，正文最多 500 + 少量元数据行
            self.assertLess(len(content), 800, "限流未生效，正文长度=%d" % len(content))

    def test_dump_never_raises(self):
        """dump 是辅助通道，自身故障绝不能把主流程带崩。"""
        import os
        import tempfile
        from unittest import mock

        class Exploding:
            @property
            def text(self):
                raise RuntimeError("body unreadable")

            status_code = 500
            url = "https://example.test/explode"

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"UPI_DUMP": "1", "UPI_DUMP_DIR": tmp}, clear=False):
                try:
                    upi_link._upi_dump_http(Exploding(), "stage_explode")
                except Exception as exc:  # pragma: no cover - 失败即测试失败
                    self.fail("dump 不应抛异常，却抛了 %s: %s" % (type(exc).__name__, exc))

    def test_dump_dir_override_is_honoured(self):
        """``UPI_DUMP_DIR`` 覆盖必须优先于默认的 ``runtime/upi_dumps``。"""
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"UPI_DUMP_DIR": tmp}, clear=False):
                resolved = str(upi_link._upi_dump_dir())
            self.assertEqual(os.path.normcase(resolved), os.path.normcase(tmp))

    def test_default_dump_dir_is_inside_gitignored_runtime(self):
        """默认目录必须落在 ``runtime/`` 下——那里被 .gitignore 整目录忽略。

        变异判据：把 ``UPI_DUMP_DEFAULT_DIR`` 改成仓库根或 ``dumps/``，
        本条变红（会把请求响应提交进仓库）。
        """
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UPI_DUMP_DIR", None)
            default_dir = upi_link._upi_dump_dir()
        repo_root = os.path.dirname(os.path.abspath(upi_link.__file__))
        repo_root = os.path.dirname(repo_root)
        self.assertTrue(
            str(default_dir).startswith(os.path.join(repo_root, "runtime")),
            "默认 dump 目录 %s 不在 runtime/ 下，会被 git 跟踪" % default_dir,
        )


class FingerprintContractTests(unittest.TestCase):
    """指纹的 locale / timezone 必须由契约层驱动（P2-1）。

    历史问题：``UPI_FINGERPRINT_TEMPLATES`` 里硬编码 ``en-IN`` / ``Asia/Kolkata``，
    与 ``checkout_contract.COUNTRY_BROWSER_PROFILES`` 构成**第二份真源**。
    越南优惠阶段需要 ``vi-VN`` / ``Asia/Ho_Chi_Minh``，硬编码会让「UA 说越南
    但 Accept-Language 说印度」这种矛盾指纹重现。

    变异判据：把 ``_upi_fingerprint`` 里 ``result["locale"] = locale`` 一行删掉，
    ``test_locale_follows_contract_layer`` 变红。
    """

    def test_locale_follows_contract_layer(self):
        """入参国家决定 locale / timezone / Accept-Language 三者同步变化。"""
        from sms_tool import checkout_contract

        for country in ("IN", "VN", "US", "GB"):
            with self.subTest(country=country):
                expected = checkout_contract.browser_profile_for_country(country)
                fp = upi_link._upi_fingerprint(0, country)
                self.assertEqual(fp["locale"], expected.browser_locale)
                self.assertEqual(fp["timezone"], expected.browser_timezone)
                primary = expected.browser_locale.split("-")[0]
                self.assertTrue(
                    fp["accept_language"].startswith(expected.browser_locale),
                    "Accept-Language 未跟随 locale: %r" % fp["accept_language"],
                )
                self.assertIn(primary, fp["accept_language"])

    def test_vietnam_does_not_inherit_india_locale(self):
        """🔴 专钉越南：必须拿到 vi-VN，不能沿用模板兜底的 en-IN。"""
        fp = upi_link._upi_fingerprint(0, "VN")
        self.assertEqual(fp["locale"], "vi-VN")
        self.assertEqual(fp["timezone"], "Asia/Ho_Chi_Minh")
        self.assertNotIn("India", fp["timezone"])
        self.assertNotIn("en-IN", fp["accept_language"])

    def test_no_country_keeps_template_fallback(self):
        """不传 country 的老调用点仍拿到一套自洽身份（模板兜底不被破坏）。"""
        fp = upi_link._upi_fingerprint(0)
        self.assertTrue(fp.get("locale"))
        self.assertTrue(fp.get("timezone"))
        self.assertTrue(fp.get("accept_language"))

    def test_contract_field_names_are_not_silently_guessed(self):
        """契约层字段缺失必须炸出来，不能静默回落到模板里的 en-IN。

        正确实现读到 ``browser_locale``，本用例给的是**不含**该字段的 profile
        ⇒ 必须抛错。若有谁把它改成 ``getattr(profile, "locale", "")`` 的静默
        兜底，就会读不到并**静默沿用模板的 en-IN**，本用例即红。
        """
        from unittest import mock

        class NoProfileFields:
            """什么字段都没有——两个分支都应判定为缺失。"""

        with mock.patch.object(
            upi_link, "browser_profile_for_country", lambda country: NoProfileFields()
        ):
            with self.assertRaises(RuntimeError) as ctx:
                upi_link._upi_fingerprint(0, "IN")
        self.assertIn("browser_profile_for_country", str(ctx.exception))

    def test_locale_field_name_is_actually_read(self):
        """🔴 专钉字段名：把 ``browser_locale`` 读成裸 ``locale`` 必须暴露。

        提供的 profile 有 ``locale``（错名字）也有 ``browser_timezone``，
        唯独没有 ``browser_locale``。正确实现读到空 → 抛「缺 locale」；
        若实现里写的是 ``getattr(profile, "locale", "")``，它会读到
        ``"WRONG"`` 并**静默通过**，本用例即红。
        """

        class TrapProfile:
            locale = "WRONG-LOCALE"
            browser_timezone = "Asia/Kolkata"
            # 故意不给 browser_locale

        from unittest import mock

        with mock.patch.object(
            upi_link, "browser_profile_for_country", lambda country: TrapProfile()
        ):
            with self.assertRaises(RuntimeError) as ctx:
                upi_link._upi_fingerprint(0, "IN")
        self.assertIn("browser_profile_for_country", str(ctx.exception))

    def test_accept_language_derivation(self):
        """``Accept-Language`` 生成规则：主标签降级链。"""
        cases = {
            "vi-VN": "vi-VN,vi;q=0.9",
            "en-IN": "en-IN,en;q=0.9",
            "en": "en;q=0.9",
            "": "en;q=0.9",
        }
        for locale, expected in cases.items():
            with self.subTest(locale=locale):
                self.assertEqual(upi_link._upi_accept_language_for(locale), expected)

    def test_fingerprint_self_consistency_survives_country_override(self):
        """覆盖 locale 后 UA / sec-ch-ua / platform 之间仍自洽。"""
        for country in ("IN", "VN", "US"):
            with self.subTest(country=country):
                fp = upi_link._upi_fingerprint(0, country)
                ua = fp["user_agent"]
                self.assertEqual(fp["sec_ch_ua_mobile"], "?0")
                if "Windows" in ua:
                    self.assertEqual(fp["sec_ch_ua_platform"], '"Windows"')
                elif "Macintosh" in ua:
                    self.assertEqual(fp["sec_ch_ua_platform"], '"macOS"')
                elif "Linux" in ua:
                    self.assertEqual(fp["sec_ch_ua_platform"], '"Linux"')
                else:  # pragma: no cover
                    self.fail("未知 UA 平台: %s" % ua)
                version = ua.rsplit("Chrome/", 1)[-1].split(".", 1)[0]
                self.assertIn('v="%s"' % version, fp["sec_ch_ua"])


class DegradeOn403Tests(unittest.TestCase):
    """403 指纹降级重试（P2-2）。

    参考实现用 ``UPI_PROMOTION_IMPERSONATE=chrome124`` 避免 VN 阶段的 chrome136
    403——即 chrome136 会被上游 WAF 拒。本项目没有独立 promote 阶段，所以改成
    「观察到 403 → 换 chrome124/macOS 身份重试**一次**」。

    三条不变量：
      1. 403 会触发重试，且重试用的是降级身份
      2. 重试**只做一次**（不能无限换身份烧配额）
      3. 非 403 的错误不触发重试（换身份没有因果，只是多打一个请求）
    """

    class _Resp:
        def __init__(self, status_code, text="body"):
            self.status_code = status_code
            self.text = text
            self.url = "https://api.stripe.com/v1/x"
            self.headers = {}

    class _Session:
        """按顺序吐响应的假 session，记录每次请求时的 UA。"""

        def __init__(self, statuses):
            self._statuses = list(statuses)
            self.headers = {}
            self.seen_uas = []
            self.calls = 0

        def post(self, url, **kwargs):
            self.seen_uas.append(self.headers.get("User-Agent", ""))
            self.calls += 1
            if self._statuses:
                status = self._statuses.pop(0)
            else:
                status = 200
            return DegradeOn403Tests._Resp(status)

        def __getattr__(self, name):
            raise AttributeError(name)

    def test_403_triggers_one_retry_with_degraded_fingerprint(self):
        session = self._Session([403, 200])
        original = upi_link._upi_fingerprint(0, "IN")
        resp, used = upi_link._upi_post_with_degrade(
            session, "https://api.stripe.com/v1/x",
            data={"a": "b"}, fingerprint=original, stage="t",
        )
        self.assertEqual(resp.status_code, 200, "重试应拿到 200")
        self.assertEqual(session.calls, 2, "应恰好请求两次（原 + 重试一次）")
        self.assertEqual(used.get("name"), "chrome-mac", "重试应换成降级身份")
        self.assertNotEqual(
            session.seen_uas[0], session.seen_uas[1],
            "重试时的 User-Agent 必须真的换了",
        )

    def test_403_retry_is_bounded_to_one(self):
        """连续 403 只重试一次，不无限换身份。"""
        session = self._Session([403, 403, 403, 403])
        original = upi_link._upi_fingerprint(0, "IN")
        resp, used = upi_link._upi_post_with_degrade(
            session, "https://api.stripe.com/v1/x",
            data={"a": "b"}, fingerprint=original, stage="t",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(session.calls, 2, "最多两次请求，不能继续换身份")

    def test_non_403_does_not_retry(self):
        """500 不触发降级——换身份对服务端错误没有因果。"""
        session = self._Session([500, 200])
        original = upi_link._upi_fingerprint(0, "IN")
        resp, used = upi_link._upi_post_with_degrade(
            session, "https://api.stripe.com/v1/x",
            data={"a": "b"}, fingerprint=original, stage="t",
        )
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(session.calls, 1, "非 403 不应重试")
        self.assertEqual(used.get("name"), original.get("name"))

    def test_already_degraded_does_not_retry(self):
        """已经在用降级身份时再 403，不再重试（同一套身份重打无意义）。"""
        session = self._Session([403, 200])
        degraded = upi_link._upi_degraded_template()
        resp, used = upi_link._upi_post_with_degrade(
            session, "https://api.stripe.com/v1/x",
            data={"a": "b"}, fingerprint=degraded, stage="t",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(session.calls, 1, "已降级时不应再重试")

    def test_is_403_recognises_waf_challenge(self):
        """``cf-mitigated: challenge`` 也算 403 降级信号。"""

        class R:
            status_code = 200
            headers = {"cf-mitigated": "challenge"}

        self.assertTrue(upi_link._upi_is_403(R()))
        self.assertTrue(upi_link._upi_is_403(self._Resp(403)))
        self.assertFalse(upi_link._upi_is_403(self._Resp(200)))
        self.assertFalse(upi_link._upi_is_403(None))

    def test_degraded_template_is_the_macos_chrome124_one(self):
        """降级模板必须是 chrome124 + macOS——这是参考实现验证过能过 WAF 的身份。"""
        template = upi_link._upi_degraded_template()
        self.assertEqual(template.get("name"), "chrome-mac")
        self.assertEqual(template.get("impersonate"), "chrome124")
        self.assertIn("Macintosh", template.get("user_agent", ""))
        self.assertEqual(template.get("sec_ch_ua_platform"), '"macOS"')

    def test_degraded_template_lookup_is_by_name_not_index(self):
        """按名字查，不按下标——模板顺序调整时取下标会静默换错身份。

        变异判据：把 ``_upi_degraded_template`` 改成
        ``return dict(UPI_FINGERPRINT_TEMPLATES[1])``，本条仍绿（当前恰好同序），
        但把模板顺序打乱后就会红——所以本用例显式断言 name 字段。
        """
        template = upi_link._upi_degraded_template()
        self.assertIn("name", template)
        self.assertEqual(template["name"], "chrome-mac")
        # 该模板在元组里的下标无关紧要，name 才是契约
        names = [t.get("name") for t in upi_link.UPI_FINGERPRINT_TEMPLATES]
        self.assertIn("chrome-mac", names)


class ZeroAmountCacheTests(unittest.TestCase):
    """0 元缓存接线（P3-3b）。

    🔴 结论先行：**缓存本身项目里已有**（``PayPalProxyState.record_zero_result``
    / ``zero_status``），本轮只是把 UPI 流水线**接上去**。三要素本来就是齐的：
      * 禁用开关：构造参数 ``enabled``
      * 存储位置：``runtime/paypal_proxy_state.json``（``runtime/`` 已 gitignore）
      * 过期策略：``zero_cache_ttl_seconds``（默认 1800s）
    """

    PROXY = "http://u:p@1.2.3.4:8080"

    def _state(self, enabled=True, ttl=1800):
        import tempfile
        from pathlib import Path

        from sms_tool.paypal_proxy import PayPalProxyState

        tmp = tempfile.mkdtemp()
        return PayPalProxyState(Path(tmp) / "state.json", enabled=enabled,
                                zero_cache_ttl_seconds=ttl)

    def test_zero_result_is_recorded_and_readable(self):
        state = self._state()
        self.assertEqual(state.zero_status(self.PROXY, "IN"), ("", None))
        upi_link._upi_record_zero_result(state, self.PROXY, "IN", 0)
        self.assertEqual(state.zero_status(self.PROXY, "IN"), ("ok", 0))
        upi_link._upi_record_zero_result(state, self.PROXY, "IN", 999)
        self.assertEqual(state.zero_status(self.PROXY, "IN"), ("bad", 999))

    def test_country_mismatch_invalidates_the_cache(self):
        """国家变了缓存必须失效——不同国家的 0 元资格不通用。"""
        state = self._state()
        upi_link._upi_record_zero_result(state, self.PROXY, "IN", 0)
        self.assertEqual(state.zero_status(self.PROXY, "IN"), ("ok", 0))
        self.assertEqual(state.zero_status(self.PROXY, "VN"), ("", None))

    def test_disabled_switch_turns_it_off(self):
        """``enabled=False`` 时既不写也不读。"""
        state = self._state(enabled=False)
        upi_link._upi_record_zero_result(state, self.PROXY, "IN", 0)
        self.assertEqual(state.zero_status(self.PROXY, "IN"), ("", None))

    def test_never_raises(self):
        """调度优化绝不能把主流程带崩。"""

        class Boom:
            def record_zero_result(self, *args):
                raise RuntimeError("boom")

        try:
            upi_link._upi_record_zero_result(Boom(), self.PROXY, "IN", 1)
        except Exception as exc:  # pragma: no cover
            self.fail("0 元缓存不应抛异常，却抛了 %s" % type(exc).__name__)

    def test_no_state_or_no_proxy_is_a_noop(self):
        """不传 proxy_state / 代理为空：完全关闭，既有调用方行为不变。

        🔴 判据必须用**探针对象**而不是真的 ``PayPalProxyState``：后者自己
        内部就有空代理守卫，用它验证等于在测下层——把本函数的守卫删掉也照样绿
        （变异实测 MISSED）。要判的是「本函数**根本没往下调用**」。
        """

        class Spy:
            def __init__(self):
                self.calls = []

            def record_zero_result(self, proxy, country, amount):
                self.calls.append((proxy, country, amount))

        spy = Spy()
        upi_link._upi_record_zero_result(None, self.PROXY, "IN", 1)
        upi_link._upi_record_zero_result(spy, "", "IN", 1)
        upi_link._upi_record_zero_result(spy, None, "IN", 1)
        self.assertEqual(spy.calls, [], "空代理/空 state 时不应往下游写")

        # 反过来：正常入参必须真的被调用，证明探针本身有效
        upi_link._upi_record_zero_result(spy, self.PROXY, "IN", 0)
        self.assertEqual(spy.calls, [(self.PROXY, "IN", 0)])

    def test_generate_accepts_optional_proxy_state(self):
        """``proxy_state`` 必须是**可选**参数——不能破坏既有调用方。"""
        import inspect

        sig = inspect.signature(upi_link.generate_upi_qr_link)
        self.assertIn("proxy_state", sig.parameters)
        self.assertIsNone(sig.parameters["proxy_state"].default)


if __name__ == "__main__":
    unittest.main()
