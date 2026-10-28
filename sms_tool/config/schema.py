"""Static validation and non-fatal advisories for the application configuration.

Split out of the loader so "what the config must contain" can be read without the
file I/O in view.  The two entry points have opposite failure contracts on
purpose: :func:`validate_config` raises :class:`ConfigError`, while
:func:`config_warnings` never raises at all -- an advisory must not be able to
break a config load.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from ..kernel.driver_env import driver_config


class ConfigError(ValueError):
    pass


_LOGGER = logging.getLogger(__name__)


# Keys under ``account_health`` that this build understands. The list is
# deliberately generous: an unrecognised key only produces a *warning* (see
# ``config_warnings``), so over-listing costs a missed typo warning while
# under-listing would nag about legitimate config.
ACCOUNT_HEALTH_KEYS = frozenset(
    {
        "use_registration_affinity",
        "batch_timeout_seconds",
        "account_timeout_seconds",
        "relogin_cooldown_seconds",
        "relogin_repeat_cooldown_seconds",
        "relogin_dead_end_permanent",
        "post_registration_enabled",
        "post_registration_deep_liveness",
        "proxy_pool",
        "proxies",
        "workers",
        "max_pending",
        "state_file",
        "checks",
        "enabled",
        "consecutive_failures",
        "fail_skip_after",
        "fail_cooldown_seconds",
        "probe_cache_ttl_seconds",
        "zero_cache_ttl_seconds",
    }
)


# ``account_health_queue`` clamps the worker count to this range, so anything
# outside it is silently ignored -- worth telling the operator about.
ACCOUNT_HEALTH_WORKER_RANGE = (1, 8)


def validate_registration_driver_config(
    config: Mapping[str, Any],
    driver: Any = None,
    *,
    proxy: Any = None,
) -> str:
    """Validate credentials required by the selected browser driver.

    Static ``validate_config`` checks types and URL shapes, but cannot require
    credentials for every optional driver.  This focused preflight is called at
    the registration boundary so a missing cloud/API setting is reported before
    a disposable mailbox is claimed or a browser session is created.
    """
    from ..registration_drivers.base import normalize_registration_driver

    selected = normalize_registration_driver(driver, config)
    if selected == "protocol":
        return selected

    _raw_registration: object = config.get("registration")
    registration = _raw_registration if isinstance(_raw_registration, Mapping) else {}
    _raw_drivers: object = registration.get("drivers")
    drivers = _raw_drivers if isinstance(_raw_drivers, Mapping) else {}
    _raw_selected_config: object = drivers.get(selected)
    selected_config = _raw_selected_config if isinstance(_raw_selected_config, Mapping) else {}
    # Use the same environment-overlay logic as the runtime session factory so
    # preflight does not reject a driver whose credentials live in deployment
    # environment variables.
    #
    # This used to import `_driver_config` from
    # `registration_drivers/external_sessions`. That made `sms_tool.config` -
    # imported by 64 modules, so effectively every import in the package -
    # depend on a browser-driver module that itself depends on config for proxy
    # and runtime settings: a cycle, and the worst possible place for one. The
    # logic moved to `sms_tool/driver_env.py`, below both consumers.
    try:
        selected_config = driver_config(config, selected)
    except Exception:
        selected_config = dict(selected_config)

    # Cloud browser secrets may be injected by deployment environments.  Keep
    # environment precedence at the validation boundary without mutating the
    # persisted JSON configuration.
    requirements = {
        "roxy": (("workspace_id", "roxy_workspace_id_missing"),),
    }
    for key, error_code in requirements.get(selected, ()):
        configured = selected_config.get(key)
        if not str(configured or "").strip():
            raise ConfigError(error_code)
    # ``proxy`` stays in the signature for call-site stability; every remaining
    # driver consumes the registration proxy locally, so there is no
    # "provider-native proxy setting required" case left to validate.
    return selected


#: Lanes accepted under ``proxy.lanes``. ``payment_qualification`` is the lane
#: the desktop's 支付资格专用代理池 field writes and the one
#: ``qualification.configured_qualification_proxy_pool`` reads through
#: ``canonical_lane_pool``; omitting it rejected the desktop's own save, so the
#: operator filled the box, the write landed, and every later backend command
#: died in ``load_runtime_config`` with "unsupported proxy lane".
_SUPPORTED_PROXY_LANES = frozenset(
    {
        "protocol_registration",
        "browser_registration",
        "mailbox",
        "payment_qualification",
        "payment",
    }
)

#: Lanes that may be named in ``proxy.allow_lane_overlap`` -- exactly the lanes
#: ``proxy.registry.acknowledged_lane_overlaps`` accepts, minus registration.
#: Registration owns the exits the others are compared *against*, so "registration
#: overlaps registration" is not a declaration, it is a no-op.
#:
#: Restated rather than imported: ``config`` sits below ``proxy`` in the layer
#: manifest, and importing the registry here would invert that edge. The two sets
#: are pinned against each other by ``tests/config/test_config_proxy_lanes_validation.py``
#: so a lane added on one side cannot be silently ignored by the other.
_OVERLAPPABLE_PROXY_LANES = frozenset({"mailbox", "payment", "payment_qualification"})


def validate_config(config: Mapping[str, Any], *, workflow: str | None = None) -> None:
    """Validate static workflow inputs before network or subprocess execution."""
    errors: list[str] = []
    chatgpt = config.get("chatgpt")
    if not isinstance(chatgpt, Mapping):
        errors.append("chatgpt must be an object")
    else:
        for key in ("auth_base_url", "chat_base_url"):
            value = str(chatgpt.get(key) or "").strip()
            if value and urlsplit(value).scheme not in {"http", "https"}:
                errors.append(f"chatgpt.{key} must be an http(s) URL")

    proxy = config.get("proxy", {})
    if proxy is not None and not isinstance(proxy, Mapping):
        errors.append("proxy must be an object")
    if isinstance(proxy, Mapping):
        pool = proxy.get("pool", [])
        if pool is not None and not isinstance(pool, (list, tuple)):
            errors.append("proxy.pool must be an array")
        for key in (
            "browser_registration_pool",
            "browser_pool",
            "protocol_registration_pool",
            "protocol_pool",
        ):
            if key in proxy and not isinstance(proxy.get(key), (str, list, tuple)):
                errors.append(f"proxy.{key} must be a proxy list")
        if "health" in proxy and not isinstance(proxy.get("health"), (str, list, tuple)):
            errors.append("proxy.health must be a proxy list")
        # ``proxy.lanes`` is the optional single declaration point that
        # ``proxy_routing.canonical_lane_pool`` treats as authoritative when it
        # is present.  Without this check a misspelled lane name or a malformed
        # pool sailed through validation and silently fell back to the legacy
        # keys, so the operator had no signal that the declared lane was dead.
        lanes = proxy.get("lanes")
        if lanes is not None:
            if not isinstance(lanes, Mapping):
                errors.append("proxy.lanes must be an object")
            else:
                supported_lanes = _SUPPORTED_PROXY_LANES
                unknown_lanes = sorted(set(lanes) - supported_lanes)
                if unknown_lanes:
                    errors.append("unsupported proxy lane: " + ", ".join(unknown_lanes))
                for lane, value in lanes.items():
                    if lane == "payment":
                        # ``{"pools": {"US": [...]}, "default": [...]}`` or a bare pool.
                        if isinstance(value, Mapping):
                            named = value.get("pools")
                            if named is not None and not isinstance(named, Mapping):
                                errors.append("proxy.lanes.payment.pools must be an object")
                            elif isinstance(named, Mapping):
                                for name, pool in named.items():
                                    if not str(name or "").strip():
                                        errors.append("proxy.lanes.payment.pools names must not be blank")
                                    if not isinstance(pool, (str, list, tuple)):
                                        errors.append(f"proxy.lanes.payment.pools.{name} must be a proxy list")
                            for key in ("default", "pool", "proxies"):
                                if key in value and not isinstance(value.get(key), (str, list, tuple)):
                                    errors.append(f"proxy.lanes.payment.{key} must be a proxy list")
                        elif not isinstance(value, (str, list, tuple)):
                            errors.append("proxy.lanes.payment must be a proxy pool or an object")
                    elif not isinstance(value, (str, list, tuple, Mapping)):
                        errors.append(f"proxy.lanes.{lane} must be a proxy list")
                    elif isinstance(value, Mapping):
                        # ``canonical_lane_pool`` reads pool/proxies/default from a mapping.
                        for key in ("pool", "proxies", "default"):
                            if key in value and not isinstance(value.get(key), (str, list, tuple)):
                                errors.append(f"proxy.lanes.{lane}.{key} must be a proxy list")
        _validate_allow_lane_overlap(proxy, errors)
    if "mailbox_proxy_pool" in config and not isinstance(config.get("mailbox_proxy_pool"), (str, list, tuple)):
        errors.append("mailbox_proxy_pool must be a proxy list")

    account_health = config.get("account_health", {})
    if account_health is not None and not isinstance(account_health, Mapping):
        errors.append("account_health must be an object")
    if isinstance(account_health, Mapping):
        if "proxy_pool" in account_health and not isinstance(account_health.get("proxy_pool"), (str, list, tuple)):
            errors.append("account_health.proxy_pool must be a proxy list")
        health_proxies = account_health.get("proxies", {})
        if health_proxies is not None and not isinstance(health_proxies, Mapping):
            errors.append("account_health.proxies must be an object")
        elif isinstance(health_proxies, Mapping):
            supported_health_lanes = {
                "liveness",
                "liveness_pool",
                "quota_pool",
                "promotion",
                "promotion_pool",
                "browser",
                "browser_pool",
                "browser_verification_pool",
            }
            unknown_health_lanes = sorted(set(health_proxies) - supported_health_lanes)
            if unknown_health_lanes:
                errors.append("unsupported account_health proxy lane: " + ", ".join(unknown_health_lanes))
            for key, value in health_proxies.items():
                if not isinstance(value, (str, list, tuple)):
                    errors.append(f"account_health.proxies.{key} must be a proxy list")
        _validate_positive_numbers(
            account_health,
            (
                "workers",
                "max_pending",
                "batch_timeout_seconds",
                "account_timeout_seconds",
                "relogin_cooldown_seconds",
                "relogin_repeat_cooldown_seconds",
                "fail_skip_after",
                "fail_cooldown_seconds",
            ),
            "account_health",
            errors,
        )

    registration = config.get("registration", {})
    if registration is not None and not isinstance(registration, Mapping):
        errors.append("registration must be an object")
    if isinstance(registration, Mapping):
        from ..registration_drivers.base import KNOWN_DRIVER_ALIASES

        if "browser_profile_pool" in registration:
            errors.append("registration.browser_profile_pool is unsupported; browser hardware profiles are built in")
        driver = str(registration.get("driver") or "protocol").strip().lower().replace("-", "_")
        if driver not in KNOWN_DRIVER_ALIASES:
            errors.append("registration.driver is unsupported")
        _validate_positive_numbers(
            registration,
            (
                "retry_attempts",
                "retry_delay_seconds",
                "at_stability_probe_count",
                "at_stability_probe_delay_seconds",
                "at_probe_timeout_seconds",
                "browser_timeout_seconds",
                "browser_worker_limit",
            ),
            "registration",
            errors,
        )
        if "browser_headless" in registration and not isinstance(registration.get("browser_headless"), bool):
            errors.append("registration.browser_headless must be a boolean")
        for key in (
            "edge_challenge_discrimination",
            "edge_challenge_rotate_exit",
            "prime_about_you_page",
            "prime_create_account_password",
            "prime_navigation_headers",
            "signup_continue_screen_hint",
            "signin_screen_hint_login_or_signup",
            "signin_prompt_login",
            "signin_locale_ja_jp",
            "signup_email_verification_continue_hint",
            "create_account_disallowed_backoff",
            "existing_login_continue_on_verified_page",
            "sentinel_password_bundle",
            "obtain_refresh_token",
            "turb_signin_authorize_context",
            "prime_password_page_fatal",
            "otp_navigation_headers",
            "otp_validate_sentinel",
            "otp_external_url_branch",
            "device_identity_seeded_by_email",
            "passwordless_explicit_send_otp",
            "reference_lane",
        ):
            if key in registration and not isinstance(registration.get(key), bool):
                errors.append(f"registration.{key} must be a boolean")
        for key in ("browser_locale", "browser_timezone"):
            if key in registration and not str(registration.get(key) or "").strip():
                errors.append(f"registration.{key} must not be blank")
        drivers = registration.get("drivers", {})
        if drivers is not None and not isinstance(drivers, Mapping):
            errors.append("registration.drivers must be an object")
        elif isinstance(drivers, Mapping):
            from ..registration_drivers.base import BROWSER_REGISTRATION_DRIVERS

            supported_drivers = BROWSER_REGISTRATION_DRIVERS
            unknown_drivers = sorted(set(drivers) - supported_drivers)
            if unknown_drivers:
                errors.append(f"unsupported registration driver config: {', '.join(unknown_drivers)}")
            for name, raw in drivers.items():
                if not isinstance(raw, Mapping):
                    errors.append(f"registration.drivers.{name} must be an object")
                    continue
                for key in ("api_base", "cdp_base", "start_url"):
                    value = str(raw.get(key) or "").strip()
                    if value and urlsplit(value).scheme not in {"http", "https", "ws", "wss"}:
                        errors.append(f"registration.drivers.{name}.{key} must be a URL")
                _validate_positive_numbers(
                    raw,
                    ("session_timeout_minutes",),
                    f"registration.drivers.{name}",
                    errors,
                )
                for key in (
                    "use_proxy",
                    "humanize",
                    "geoip",
                    "keep_browser_open",
                    "delete_profile_after_run",
                    "generate_browser_profile",
                    "ad_blocker",
                ):
                    if key in raw and not isinstance(raw.get(key), bool):
                        errors.append(f"registration.drivers.{name}.{key} must be a boolean")
        # Per-stage wall-clock ceilings (seconds). No shipped config shard sets
        # this key yet, so the branch normally runs against an empty mapping and
        # reports nothing -- that is expected, not dead code: it is the guard
        # that fires the moment an operator configures a stage timeout.
        # `config.example.json` carries an empty `stage_timeouts` object so the
        # supported stage names are discoverable; the authoritative list is the
        # `valid_stages` set right below.
        stage_timeouts = registration.get("stage_timeouts", {})
        if stage_timeouts is not None and not isinstance(stage_timeouts, Mapping):
            errors.append("registration.stage_timeouts must be an object")
        elif isinstance(stage_timeouts, Mapping):
            valid_stages = {
                "sentinel",
                "identity_ready",
                "auth_flow",
                "user_register",
                "email_otp_send",
                "email_otp_wait",
                "email_otp_validate",
                "create_account",
                "auth_session",
                "codex_oauth",
                "access_token_probe",
                "totp_enroll",
                "finalize",
            }
            unknown_stages = sorted(set(stage_timeouts) - valid_stages)
            if unknown_stages:
                errors.append(f"unsupported registration stage timeout: {', '.join(unknown_stages)}")
            _validate_positive_numbers(
                stage_timeouts,
                tuple(str(key) for key in stage_timeouts),
                "registration.stage_timeouts",
                errors,
            )
        pulse = registration.get("pulse", {})
        if pulse is not None and not isinstance(pulse, Mapping):
            errors.append("registration.pulse must be an object")
        elif isinstance(pulse, Mapping):
            if "enabled" in pulse and not isinstance(pulse.get("enabled"), bool):
                errors.append("registration.pulse.enabled must be a boolean")
            if "canary_enabled" in pulse and not isinstance(pulse.get("canary_enabled"), bool):
                errors.append("registration.pulse.canary_enabled must be a boolean")
            _validate_positive_numbers(
                pulse,
                ("wave_size", "wave_delay_seconds", "ban_threshold", "ban_pause_seconds", "max_waves"),
                "registration.pulse",
                errors,
            )
        retry_policy = registration.get("retry_policy", {})
        if retry_policy is not None and not isinstance(retry_policy, Mapping):
            errors.append("registration.retry_policy must be an object")
        elif isinstance(retry_policy, Mapping):
            for key in ("cross_batch_cooldown_seconds", "otp_pending_quarantine_threshold"):
                if key not in retry_policy:
                    continue
                value = retry_policy.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                    errors.append(f"registration.retry_policy.{key} must be a positive number")
        process_pool = registration.get("browser_process_pool", {})
        if process_pool is not None and not isinstance(process_pool, Mapping):
            errors.append("registration.browser_process_pool must be an object")
        elif isinstance(process_pool, Mapping):
            # NOTE: deliberately NOT named ``browser_pool`` -- that key is a
            # proxy-pool alias (see proxy_routing.PROXY_LANE_ALIASES); reusing
            # it here would make every future grep ambiguous.
            for key in ("enabled", "recycle_on_error"):
                if key in process_pool and not isinstance(process_pool.get(key), bool):
                    errors.append(f"registration.browser_process_pool.{key} must be a boolean")
            _validate_positive_numbers(
                process_pool,
                ("max_concurrent", "max_uses_per_process"),
                "registration.browser_process_pool",
                errors,
            )
        # Protocol fingerprint pool (``sms_tool/fingerprint_pool.py``). This
        # key was read by ``FingerprintPool.from_config`` but absent from the
        # template, the shard manifest and this validator, so the pool's
        # ``allowed_countries``/``verify_hint`` knobs existed without a
        # discoverable configuration surface. Validated here so a typo or a
        # wrong type fails loudly instead of silently falling back to the
        # default pool.
        fingerprint_pool = registration.get("fingerprint_pool", {})
        if fingerprint_pool is not None and not isinstance(fingerprint_pool, Mapping):
            errors.append("registration.fingerprint_pool must be an object")
        elif isinstance(fingerprint_pool, Mapping):
            mode = fingerprint_pool.get("mode")
            if mode is not None and str(mode).strip().lower() not in {"random", "round_robin"}:
                errors.append("registration.fingerprint_pool.mode must be 'random' or 'round_robin'")
            if "verify_hint" in fingerprint_pool and not isinstance(fingerprint_pool.get("verify_hint"), bool):
                errors.append("registration.fingerprint_pool.verify_hint must be a boolean")
            countries = fingerprint_pool.get("allowed_countries")
            if countries is not None:
                if not isinstance(countries, (list, tuple)):
                    errors.append("registration.fingerprint_pool.allowed_countries must be an array")
                else:
                    invalid_countries = sorted(
                        str(code)
                        for code in countries
                        if str(code or "").strip() and not re.fullmatch(r"[A-Za-z]{2}", str(code).strip())
                    )
                    if invalid_countries:
                        errors.append(
                            "registration.fingerprint_pool.allowed_countries entries must be "
                            "two-letter ISO country codes: " + ", ".join(invalid_countries)
                        )

    email = config.get("email_registration", {})
    if email is not None and not isinstance(email, Mapping):
        errors.append("email_registration must be an object")
    if isinstance(email, Mapping):
        _validate_positive_numbers(
            email,
            (
                "otp_timeout",
                "otp_poll_interval",
                "sentinel_circuit_cooldown_seconds",
                "sentinel_circuit_failures",
                "sentinel_max_concurrency",
                "sentinel_prewarm_window",
            ),
            "email_registration",
            errors,
        )
        # URL-shaped fields: a malformed URL used to sail through validation and
        # only fail at first use, deep in a registration batch.
        for key in ("cfworker_url", "graph_messages_url", "oauth_token_url"):
            value = str(email.get(key) or "").strip()
            if value and urlsplit(value).scheme not in {"http", "https"}:
                errors.append(f"email_registration.{key} must be an http(s) URL")
        if "use_as_username" in email and not isinstance(email.get("use_as_username"), bool):
            errors.append("email_registration.use_as_username must be a boolean")
        for key in ("gmail", "remail", "smailr"):
            if key in email and not isinstance(email.get(key), Mapping):
                errors.append(f"email_registration.{key} must be an object")

    codex_oauth = config.get("codex_oauth", {})
    if codex_oauth is not None and not isinstance(codex_oauth, Mapping):
        errors.append("codex_oauth must be an object")
    elif isinstance(codex_oauth, Mapping):
        # Optional whole-flow ceiling retained for compatibility with older
        # runtime shards; stage-specific budgets remain authoritative when set.
        # Empty-string values in legacy shards mean "unset".
        _ = codex_oauth.get("registration_timeout")
        for key in ("browser_style_send_otp", "password_first_relogin"):
            if key in codex_oauth and not isinstance(codex_oauth.get(key), bool):
                errors.append(f"codex_oauth.{key} must be a boolean")

    payments = config.get("protocol_payments", {})
    if payments is not None and not isinstance(payments, Mapping):
        errors.append("protocol_payments must be an object")
    if isinstance(payments, Mapping):
        _validate_payment_config(payments, errors)

    if workflow and workflow not in {"registration", "protocol_payments", "payment", "mailbox", "storage"}:
        errors.append(f"unknown workflow: {workflow}")
    if errors:
        raise ConfigError("; ".join(errors))


def _validate_allow_lane_overlap(proxy: Mapping[str, Any], errors: list[str]) -> None:
    """Validate ``proxy.allow_lane_overlap`` (lane names only, no unknown names).

    The key records that a lane may reuse a registration exit on purpose:
    ``proxy.registry.lane_isolation`` turns such an overlap from a *conflict* into
    a *declared decision*, so the census stops warning without the overlap itself
    disappearing.

    Validated with the same reasoning as ``proxy.lanes`` above: the reader ignores
    names it does not recognise, so a typo would leave the operator believing an
    overlap was acknowledged while the census kept flagging it — and that warning
    is the only thing standing between a reused ChatGPT exit and a Stripe
    preflight that cannot pass. Unknown names are refused here rather than
    silently dropped.

    Split out of ``validate_config`` to keep that function inside its
    function-length budget (``scripts/function_length_ratchet.py``); the block is
    self-contained and has one caller.
    """
    allowed_overlap = proxy.get("allow_lane_overlap")
    if allowed_overlap is None:
        return
    if isinstance(allowed_overlap, Mapping):
        declared = list(allowed_overlap)
    elif isinstance(allowed_overlap, str):
        declared = [item for item in re.split(r"[,;]", allowed_overlap)]
    elif isinstance(allowed_overlap, (list, tuple)):
        declared = list(allowed_overlap)
    else:
        errors.append("proxy.allow_lane_overlap must be an array of lane names")
        declared = []
    unknown_overlaps = sorted(
        {str(name).strip().lower() for name in declared if str(name).strip().lower() not in _OVERLAPPABLE_PROXY_LANES}
    )
    if unknown_overlaps:
        errors.append("unsupported proxy.allow_lane_overlap lane: " + ", ".join(unknown_overlaps))


def _log_config_warnings(config: Mapping[str, Any]) -> None:
    """Emit advisories. A diagnostic must never break config loading."""
    try:
        for message in config_warnings(config):
            _LOGGER.warning("%s", message)
    except Exception:  # pragma: no cover - defensive
        pass


def config_warnings(config: Mapping[str, Any]) -> list[str]:
    """Non-fatal advisories for config that parses fine but will not do what it says.

    Kept out of :func:`validate_config` on purpose. Every message here is a
    "you probably meant something else", not a "this cannot run": an unknown
    key may simply be a section this build has not caught up with, and raising
    over it would break an otherwise working install on a config upgrade.

    The one genuinely surprising case is ``account_health.workers`` -- the queue
    clamps it to :data:`ACCOUNT_HEALTH_WORKER_RANGE`, so a value outside that
    range is accepted, stored, and then quietly ignored.
    """
    warnings_out: list[str] = []
    health = config.get("account_health")
    if not isinstance(health, Mapping):
        return warnings_out
    unknown = sorted(str(key) for key in set(health) - ACCOUNT_HEALTH_KEYS)
    if unknown:
        warnings_out.append("unknown account_health key(s): " + ", ".join(unknown))
    low, high = ACCOUNT_HEALTH_WORKER_RANGE
    workers = health.get("workers")
    if isinstance(workers, (int, float)) and not isinstance(workers, bool):
        # pi-lens-ignore: unchecked-throwing-call-python
        if not (low <= int(workers) <= high):
            # pi-lens-ignore: unchecked-throwing-call-python
            clamped = max(low, min(int(workers), high))
            warnings_out.append(
                f"account_health.workers={workers} is outside the effective range {low}..{high}; it runs as {clamped}"
            )
    return warnings_out


def _validate_positive_numbers(
    section: Mapping[str, Any], keys: tuple[str, ...], prefix: str, errors: list[str]
) -> None:
    for key in keys:
        if key not in section:
            continue
        value = section.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            errors.append(f"{prefix}.{key} must be a non-negative number")


def _validate_payment_config(section: Mapping[str, Any], errors: list[str]) -> None:
    from ..payment.catalog import PAYMENT_CATALOG, normalize_payment_method
    from ..payment.flow import normalize_payment_stage

    supported = set(PAYMENT_CATALOG.methods)
    enabled = section.get("enabled_methods", [])
    if enabled is not None and not isinstance(enabled, (list, tuple)):
        errors.append("protocol_payments.enabled_methods must be an array")
    elif enabled:
        unknown = sorted({str(item) for item in enabled} - supported)
        if unknown:
            errors.append(f"unsupported protocol payment methods: {', '.join(unknown)}")
    methods = section.get("methods", {})
    proxy_pools = section.get("proxy_pools", {})
    if proxy_pools is not None and not isinstance(proxy_pools, Mapping):
        errors.append("protocol_payments.proxy_pools must be an object")
        proxy_pools = {}
    elif isinstance(proxy_pools, Mapping):
        for name, value in proxy_pools.items():
            if not str(name or "").strip():
                errors.append("protocol_payments.proxy_pools names must not be blank")
            if not isinstance(value, (str, list, tuple, Mapping)):
                errors.append(f"protocol_payments.proxy_pools.{name} must be a proxy list")
    if methods is not None and not isinstance(methods, Mapping):
        errors.append("protocol_payments.methods must be an object")
    elif isinstance(methods, Mapping):
        unknown = sorted(set(methods) - supported)
        if unknown:
            errors.append(f"unsupported protocol payment method config: {', '.join(unknown)}")
        known_pools = set(proxy_pools) if isinstance(proxy_pools, Mapping) else set()
        for method, raw in methods.items():
            if not isinstance(raw, Mapping):
                errors.append(f"protocol_payments.methods.{method} must be an object")
                continue
            flow_profile = raw.get("flow_profile")
            if flow_profile is not None and not str(flow_profile or "").strip():
                errors.append(f"protocol_payments.methods.{method}.flow_profile must not be blank")
            stages = raw.get("stages")
            if stages is not None and not isinstance(stages, (list, tuple)):
                errors.append(f"protocol_payments.methods.{method}.stages must be an array")
            elif isinstance(stages, (list, tuple)):
                invalid = [str(stage) for stage in stages if not normalize_payment_stage(stage)]
                if invalid:
                    errors.append(
                        f"protocol_payments.methods.{method}.stages contains unsupported stages: {', '.join(invalid)}"
                    )
            routes = raw.get("stage_routes")
            if routes is not None and not isinstance(routes, Mapping):
                errors.append(f"protocol_payments.methods.{method}.stage_routes must be an object")
            elif isinstance(routes, Mapping):
                for stage, route in routes.items():
                    prefix = f"protocol_payments.methods.{method}.stage_routes.{stage}"
                    if not normalize_payment_stage(stage):
                        errors.append(f"{prefix} uses an unsupported stage")
                        continue
                    route_value = route if isinstance(route, Mapping) else {"pool": route}
                    pool = str(route_value.get("pool") or "").strip()
                    if pool and pool not in known_pools and pool not in {"checkout", "approve", "default"}:
                        errors.append(f"{prefix}.pool references unknown proxy pool: {pool}")
                    country = str(route_value.get("country") or "").strip()
                    if country and (len(country) != 2 or not country.isalpha()):
                        errors.append(f"{prefix}.country must be ISO alpha-2")
    _validate_positive_numbers(section, ("timeout_seconds",), "protocol_payments", errors)
    matrix = section.get("matrix", {})
    if matrix is not None and not isinstance(matrix, Mapping):
        errors.append("protocol_payments.matrix must be an object")
    elif isinstance(matrix, Mapping):
        cells = matrix.get("cells", [])
        if cells is not None and not isinstance(cells, (list, tuple)):
            errors.append("protocol_payments.matrix.cells must be an array")
        names: set[str] = set()
        for index, cell in enumerate(cells or []):
            if not isinstance(cell, Mapping):
                errors.append(f"protocol_payments.matrix.cells[{index}] must be an object")
                continue
            name = str(cell.get("name") or "").strip()
            if not name:
                errors.append(f"protocol_payments.matrix.cells[{index}].name is required")
            elif name in names:
                errors.append(f"duplicate protocol payment matrix cell name: {name}")
            names.add(name)
            method = normalize_payment_method(cell.get("payment_method"), default_for_blank=False)
            if not method:
                errors.append(f"protocol_payments.matrix.cells[{index}].payment_method is unsupported")
            sample_size = cell.get("sample_size", 1)
            if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size < 1:
                errors.append(f"protocol_payments.matrix.cells[{index}].sample_size must be a positive integer")
            for key in sorted(key for key in cell if str(key).endswith("_country")):
                value = str(cell.get(key) or "").strip()
                if value and (len(value) != 2 or not value.isalpha()):
                    errors.append(f"protocol_payments.matrix.cells[{index}].{key} must be ISO alpha-2")
            if method:
                checkout_country = str(cell.get("checkout_country") or "").strip().upper()
                expected_country = PAYMENT_CATALOG.methods[method].country
                if checkout_country and checkout_country != expected_country:
                    errors.append(
                        f"protocol_payments.matrix.cells[{index}].checkout_country must be {expected_country} for {method}"
                    )
