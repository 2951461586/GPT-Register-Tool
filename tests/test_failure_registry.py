"""失败词汇单一注册表：派生一致性与分类语义保持。"""

from sms_tool import error_classification, failure_registry
from sms_tool.error_classification import (
    ACCOUNT_ERROR_MARKERS,
    AUTH_STATE_ERROR_MARKERS,
    CANCELLED_ERROR_MARKERS,
    CONFIGURATION_ERROR_MARKERS,
    CURL_TRANSPORT_MARKERS,
    INTERNAL_ERROR_MARKERS,
    MAILBOX_ERROR_MARKERS,
    NETWORK_ERROR_MARKERS,
    RATE_LIMIT_ERROR_MARKERS,
    TERMINAL_ERROR_MARKERS,
    classify_error,
)
from sms_tool.failure_registry import ADVICE, BATCH_DROPPED_CLASSES, BATCH_RETRY_CLASSES, FAILURE_CLASSES
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
    assert RETRYABLE_CLASSES == {cls.code for cls in FAILURE_CLASSES if cls.retryable}
    assert RETRYABLE_CLASSES == {"network", "auth_state"}
    assert BATCH_RETRY_CLASSES == {"network", "mailbox", "auth_state", "rate_limit"}
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


def test_curl_demotion_of_wrapped_internal_errors_is_preserved():
    # internal 包裹的 transport 失败：只有 curl 错误码能证明 transport。
    wrapped = "registration_internal_error:RuntimeError:Failed to perform, curl: (35) ..."
    assert classify_error(wrapped) == "network"
    # 没有 curl 码的 internal 文本保持 internal，不被泛网络词误降级。
    assert classify_error("registration_internal_error:NameError: name 'connection_pool' is not defined") == "internal"


def test_terminal_markers_are_orthogonal_to_classes():
    assert "http_429" in TERMINAL_ERROR_MARKERS
    assert "manual_challenge_required" in TERMINAL_ERROR_MARKERS


def test_curl_markers_are_derived_from_the_network_class():
    assert CURL_TRANSPORT_MARKERS == tuple(m for m in NETWORK_ERROR_MARKERS if m.startswith("curl:"))
    assert CURL_TRANSPORT_MARKERS
