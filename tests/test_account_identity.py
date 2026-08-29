from unittest.mock import patch

from unittest.mock import Mock

from sms_tool import auth_headers
from sms_tool.account_identity import (
    bind_account_identity,
    create_registration_identity,
    resolve_account_proxy,
)
from sms_tool.registration_handlers import RegistrationEmailWorkflow
from sms_tool.session_builder import build_session_file


def test_account_identity_reuses_registration_proxy_session_and_fingerprint():
    base_proxy = "http://user-region-US-sid-OLD1234-t-5:secret@proxy.example:443"
    registration_proxy = "http://user-region-US-sid-NEW5678-t-5:secret@proxy.example:443"
    config = {
        "proxy": {
            "registration": base_proxy,
            "pool": [base_proxy],
        },
    }

    identity = create_registration_identity(
        registration_proxy,
        pool_index=0,
        fingerprint_key="chrome146",
        device_id="device-123",
        auth_session_logging_id="session-456",
    )

    assert identity["fingerprint_key"] == "chrome146"
    assert identity["device_id"] == "device-123"
    assert identity["proxy_affinity"]["session_id"] == "NEW5678"
    assert "secret" not in str(identity)
    assert resolve_account_proxy(
        {"identity_context": identity},
        fallback_proxy="http://127.0.0.1:7897",
        config=config,
    ) == registration_proxy

    bind_account_identity(identity)
    assert auth_headers.current_auth_fingerprint()["name"] == "chrome146"


def test_account_identity_falls_back_only_for_legacy_accounts():
    fallback = "http://127.0.0.1:7897"

    assert resolve_account_proxy({}, fallback_proxy=fallback, config={}) == fallback


def test_protocol_registration_result_carries_account_identity():
    base_proxy = "http://user-region-US-sid-OLD1234-t-5:proxy-secret@proxy.example:443"
    registration_proxy = "http://user-region-US-sid-NEW5678-t-5:proxy-secret@proxy.example:443"
    config = {"proxy": {"registration": base_proxy, "pool": [base_proxy]}}
    machine = Mock()
    machine.snapshot.return_value = {"state": "completed"}
    operations = Mock()
    operations._sanitize_text.side_effect = lambda value: str(value or "")
    operations._oauth_result_summary.return_value = {}
    operations._timing_summary.return_value = {}
    operations._mailbox_snapshot.return_value = {}
    operations._retain_registration_checkpoint.return_value = True
    workflow = RegistrationEmailWorkflow(
        machine,
        config=config,
        operations=operations,
    )
    workflow.runtime.proxy = registration_proxy
    workflow.runtime.username = "protocol@example.com"
    workflow.runtime.registration_mode = "at_only"
    workflow.runtime.device_id = "device-protocol"
    workflow.runtime.session_logging_id = "logging-protocol"
    workflow.runtime.access_token = "at-secret"
    workflow.runtime.success = True
    auth_headers.set_auth_fingerprint("chrome146")

    result = workflow.finalize()

    identity = result["identity_context"]
    assert identity["fingerprint_key"] == "chrome146"
    assert identity["device_id"] == "device-protocol"
    assert identity["auth_session_logging_id"] == "logging-protocol"
    assert identity["proxy_affinity"]["session_id"] == "NEW5678"
    assert "proxy-secret" not in str(identity)
    assert resolve_account_proxy(result, fallback_proxy=None, config=config) == registration_proxy


def test_session_file_preserves_registration_identity_context():
    registration_proxy = "http://user-region-GB-sid-SAVED5678-t-5:proxy-secret@proxy.example:443"
    identity = create_registration_identity(
        registration_proxy,
        pool_index=1,
        fingerprint_key="chrome146",
        device_id="device-session",
    )

    session = build_session_file({
        "email": "session@example.com",
        "access_token": "at-secret",
        "identity_context": identity,
    })

    assert session["identity_context"] == identity
    assert "proxy-secret" not in str(session["identity_context"])


def test_registration_identity_allocates_canonical_profile_from_shared_pool():
    from sms_tool.auth_headers import AUTH_FINGERPRINT_PROFILES

    # Use a profile that exists in the canonical table so the canonicalization
    # step accepts the pool-allocated name regardless of which versions the
    # table currently ships.
    pool_profile_name = next(iter(AUTH_FINGERPRINT_PROFILES))
    profile = type("Profile", (), {"name": pool_profile_name})()
    pool = type("Pool", (), {"next": lambda self, proxy=None: profile})()

    with patch("sms_tool.account_identity.shared_fingerprint_pool", return_value=pool):
        identity = create_registration_identity(
            "http://proxy.example:8080",
            config={"registration": {"fingerprint_pool": {}}},
        )

    assert identity["fingerprint_key"] == pool_profile_name
