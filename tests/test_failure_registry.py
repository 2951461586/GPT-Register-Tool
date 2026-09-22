"""失败词汇单一注册表：派生一致性与分类语义保持。"""

from sms_tool import error_classification, failure_registry
from sms_tool.error_classification import (
    ACCOUNT_ERROR_MARKERS,
    AUTH_STATE_ERROR_MARKERS,
    CANCELLED_ERROR_MARKERS,
    CONFIGURATION_ERROR_MARKERS,
    CURL_TRANSPORT_MARKERS,
    GENERIC_TRANSPORT_MARKERS,
    INTERNAL_ERROR_MARKERS,
    MAILBOX_ERROR_MARKERS,
    NETWORK_ERROR_MARKERS,
    RATE_LIMIT_ERROR_MARKERS,
    TERMINAL_ERROR_MARKERS,
    classify_error,
)
from sms_tool.failure_registry import (
    ADVICE,
    BATCH_DROPPED_CLASSES,
    BATCH_RETRY_CLASSES,
    FAILURE_CLASSES,
    FUTURE_BATCH_CLASSES,
)
from sms_tool.registration_policy import RETRYABLE_CLASSES
from sms_tool.registration_pulse import _OTP_BAN_MARKERS


def test_error_classification_tuples_are_registry_views():
    by_code = {cls.code: cls.markers for cls in FAILURE_CLASSES}
    assert CANCELLED_ERROR_MARKERS == by_code["cancelled"]
    assert INTERNAL_ERROR_MARKERS == by_code["internal"]
    assert CONFIGURATION_ERROR_MARKERS == by_code["configuration"]
    assert ACCOUNT_ERROR_MARKERS == by_code["account"]
    assert MAILBOX_ERROR_MARKERS == by_code["mailbox"]
    assert RATE_LIMIT_ERROR_MARKERS == by_code["rate_limit"]
    assert NETWORK_ERROR_MARKERS == by_code["network"]
    assert AUTH_STATE_ERROR_MARKERS == by_code["auth_state"]
    assert TERMINAL_ERROR_MARKERS == failure_registry.TERMINAL_ERROR_MARKERS


def test_batch_and_policy_and_pulse_derive_from_the_registry():
    assert RETRYABLE_CLASSES == {
        cls.code for cls in FAILURE_CLASSES if cls.attempt_retryable
    }
    assert RETRYABLE_CLASSES == {"network", "auth_state"}
    assert FUTURE_BATCH_CLASSES == {
        cls.code for cls in FAILURE_CLASSES if cls.retain_for_future_batch
    }
    # ``upi_payment`` 加入 2026-09-17：被拒的是**这一轮 checkout**
    # （generic_decline 是 Stripe 风控对本次交易的裁决；提链超时是瞬时的），
    # 邮箱地址本身没有被消耗 ⇒ 后续批次应当重新考虑，所以进
    # ``retain_for_future_batch``。
    #
    # 但它**不进** ``attempt_retryable``：同账号立即重跑同一条已经失败的
    # 提链只会再烧一个 checkout 会话，不会改变风控裁决。
    assert FUTURE_BATCH_CLASSES == {
        "network", "mailbox", "auth_state", "rate_limit", "upi_payment"
    }
    assert BATCH_RETRY_CLASSES == FUTURE_BATCH_CLASSES
    assert BATCH_DROPPED_CLASSES == {"account"}
    assert _OTP_BAN_MARKERS == failure_registry.OTP_BAN_MARKERS


def test_advice_is_the_single_source_for_policy():
    # registration_policy._ADVICE 曾是独立副本；现在两者同源。
    from sms_tool import registration_policy

    assert registration_policy._ADVICE is ADVICE


def test_classification_precedence_is_preserved():
    # cancelled 先于 internal 先于其余类别。
    assert classify_error("registration_cancelled by NameError: x") == "cancelled"
    assert classify_error("registration_internal_error:KeyError: 'x'") == "internal"
    assert classify_error("invalid_configuration") == "configuration"
    assert classify_error("account has been deactivated") == "account"
    assert classify_error("mailbox_auth_invalid") == "mailbox"
    assert classify_error("HTTP 429 too many requests") == "rate_limit"
    assert classify_error("connection reset by peer") == "network"
    assert classify_error("browser_email_verification_stuck") == "auth_state"
    assert classify_error("nothing recognizable here") == "unknown"
    # UPI 提链专有说法（2026-09-17 补进注册表；此前全部落 unknown，
    # 而 unknown 在重试守卫眼里等同终态）。
    assert classify_error("generic_decline") == "upi_payment"
    assert classify_error("all 5 attempts blocked (provider risk control)") == "upi_payment"
    assert classify_error("submission_attempt_failed") == "upi_payment"
    assert classify_error("checkout_not_active_session") == "upi_payment"
    # 🔴 关键判据：``upi_redirect_timeout`` 只含裸词 "timeout"，会被 network
    # 的 GENERIC_TRANSPORT_MARKERS 降级机制抢走；显式登记后必须仍归 upi_payment。
    assert classify_error("upi_redirect_timeout") == "upi_payment"
    # 但**纯**网络故障仍归 network，不能被新类吃掉。
    assert classify_error("connection reset by peer") == "network"
    assert classify_error("curl: (35) ssl connect error") == "network"


def test_curl_demotion_of_wrapped_internal_errors_is_preserved():
    # internal 包裹的 transport 失败：只有 curl 错误码能证明 transport。
    wrapped = "registration_internal_error:RuntimeError:Failed to perform, curl: (35) ..."
    assert classify_error(wrapped) == "network"
    # 没有 curl 码的 internal 文本保持 internal，不被泛网络词误降级。
    assert classify_error("registration_internal_error:NameError: name 'connection_pool' is not defined") == "internal"


def test_transport_curl_codes_cover_the_ones_seen_in_the_wild():
    """2026-09-18：``curl: (56)`` 曾漏登记 ⇒ 代理建连中断被读成终态。

    实测批次 29896（最新一轮协议注册）：``registration_internal_error:
    RuntimeError:Failed to perform, curl: (56) Proxy CONNECT aborted`` 被判
    ``internal`` ⇒ **不重试、不进重试守卫、不触发 pulse 熔断**，两个 run 被
    静默吞掉（日志里查得到、守卫里查不到）。同批的 ``curl: (35)`` 正常归
    ``network``，证明缺的就是清单里的登记项本身。

    ``CURL_TRANSPORT_MARKERS`` 是从 network 类**派生**的
    （``m.startswith("curl:")``），所以本用例真正钉住的是「清单里有没有这一项」：
    删掉任意一项，对应的 ``classify_error`` 断言必须变红。
    """
    for code in ("(35)", "(28)", "(6)", "(7)", "(52)", "(56)", "(18)"):
        live = (
            "registration_internal_error:RuntimeError:Failed to perform, "
            f"curl: {code} See https://curl.se/libcurl/c/libcurl-errors.html"
        )
        assert classify_error(live) == "network", live
        assert f"curl: {code}" in CURL_TRANSPORT_MARKERS, code
        # 决定性标记：不能落进「泛化弱证据」，否则会被更具体的类抢走。
        assert f"curl: {code}" not in GENERIC_TRANSPORT_MARKERS, code

    # 反向：没有 curl 码的 internal 文本必须保持 internal（不得被泛网络词降级）。
    assert (
        classify_error(
            "registration_internal_error:NameError: name 'connection_pool' is not defined"
        )
        == "internal"
    )
    # 反向：登记 curl 码不能把「裸 proxy 词」的语义一起抬高成 decisive。
    assert classify_error("proxy") == "network"


def test_terminal_markers_are_orthogonal_to_classes():
    assert "http_429" in TERMINAL_ERROR_MARKERS
    assert "manual_challenge_required" in TERMINAL_ERROR_MARKERS


def test_curl_markers_are_derived_from_the_network_class():
    assert CURL_TRANSPORT_MARKERS == tuple(m for m in NETWORK_ERROR_MARKERS if m.startswith("curl:"))
    assert CURL_TRANSPORT_MARKERS


def test_known_registration_failures_are_not_left_unknown():
    """L3 (2026-09-13): these were answered ``unknown``, which reads as terminal.

    ``unknown`` is not in ``RETRYABLE_CLASSES``, so ``RegistrationRetryGuard``
    never accumulated a cooldown for them and the same mailbox was re-attempted
    immediately. Measured over 09-08..09-13: 18 of 179 failures were
    ``missing_auth_session_access_token``, every one of them ``unknown``.
    """
    for code in (
        "missing_auth_session_access_token",
        "browser_passwordless_otp_state_unknown",
        "browser_email_value_mismatch",
    ):
        assert classify_error(code) == "auth_state", code
        assert classify_error(code) in RETRYABLE_CLASSES, code


def test_a_stage_scoped_code_beats_a_bare_transport_word():
    """``browser_profile_submit_timeout`` contains the generic marker ``timeout``.

    NETWORK is tested before AUTH_STATE in the registry order, so the bare word
    used to win even though the browser lane
    (``session._browser_failure_class``) answers ``auth_state`` for that code.
    """
    assert classify_error("browser_profile_submit_timeout") == "auth_state"


def test_sentinel_shortfall_codes_are_retryable_and_retained():
    """P1-b (2026-09-18): these three were ``unknown``, i.e. terminal.

    The 2026-09-17 batch failed 126/126; the 42 accounts that reached
    ``create_account`` all reported ``sentinel_legacy_incomplete:
    oauth_create_account``.  Because the marker was unregistered the classifier
    answered ``unknown``, ``RegistrationRetryGuard`` never accumulated a
    cooldown, and the addresses were dropped instead of being kept for a later
    batch -- which is exactly what they needed once the corrupt vendored asset
    was repaired.
    """
    for code in (
        "sentinel_legacy_incomplete:oauth_create_account",
        "sentinel_legacy_incomplete:username_password_create",
        "sentinel_issue_failed:SentinelBundleError",
        "sentinel_fallback_incomplete:oauth_create_account:RuntimeError",
    ):
        assert classify_error(code) == "network", code
        assert classify_error(code) in RETRYABLE_CLASSES, code

    # 完整的线上错误串（带 transport 前缀、且根因后缀里含路径与冒号）也必须命中。
    live = (
        "create_account_transport:sentinel_fallback_incomplete:oauth_create_account:"
        "SentinelBundleError(sentinel_runtime_hash_mismatch:sentinel-runner.js)"
    )
    assert classify_error(live) == "network", live
    assert "network" in BATCH_RETRY_CLASSES


def test_sentinel_markers_are_decisive_not_generic_transport_words():
    """Underscored markers must never be demoted to generic vocabulary.

    ``GENERIC_TRANSPORT_MARKERS`` is ``marker.isalpha()``; a compound token such
    as ``sentinel_legacy_incomplete`` names a specific condition, so it stays
    decisive on its own.  If it were treated as generic, a more specific class
    tested later could steal it.
    """
    for marker in (
        "sentinel_legacy_incomplete",
        "sentinel_issue_failed",
        "sentinel_fallback_incomplete",
    ):
        assert marker in NETWORK_ERROR_MARKERS, marker
        assert marker not in GENERIC_TRANSPORT_MARKERS, marker
        assert not marker.startswith("curl:"), marker


def test_bare_transport_words_still_classify_as_network():
    """Negative guard for the rule above.

    Deferring the generic tokens must not turn a real transport failure into
    ``unknown`` -- that would flip it from retryable to terminal.
    """
    assert classify_error("timeout") == "network"
    assert classify_error("proxy") == "network"
    assert classify_error("connection") == "network"
    assert (
        classify_error(
            "ReadTimeout: HTTPSConnectionPool(host='auth.openai.com', port=443): "
            "Read timed out. (read timeout=10)"
        )
        == "network"
    )


def test_generic_transport_markers_are_derived_from_the_network_class():
    assert GENERIC_TRANSPORT_MARKERS == tuple(
        m for m in NETWORK_ERROR_MARKERS if m.isalpha()
    )
    assert GENERIC_TRANSPORT_MARKERS
    # A specific condition must never be treated as generic vocabulary, or it
    # would stop being decisive on its own.
    assert "session_circuit_open" not in GENERIC_TRANSPORT_MARKERS
    assert "connection reset" not in GENERIC_TRANSPORT_MARKERS
    assert not any(marker.startswith("curl:") for marker in GENERIC_TRANSPORT_MARKERS)


def test_browser_lane_and_shared_classifier_agree_on_these_codes():
    """Two classifiers answering differently is how the next divergence hides."""
    from sms_tool.registration_drivers.browser_flow.session import _browser_failure_class

    for code in (
        "browser_profile_submit_timeout",
        "browser_passwordless_otp_state_unknown",
        "browser_email_value_mismatch",
    ):
        assert _browser_failure_class(code) == classify_error(code) == "auth_state", code
