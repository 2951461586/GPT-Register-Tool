"""Single authority for protocol-payment proxy pools and stage routing."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .payment_catalog import PAYMENT_METHODS, normalize_payment_method
from .payment_flow import PaymentStage, STAGE_ORDER, normalize_payment_stage, payment_flow_profile
from .proxy_entry import parse_proxy_list, resolve_proxy_value


LEGACY_STAGE_KEYS: dict[str, tuple[str, ...]] = {
    PaymentStage.AUTH_GATE.value: ("auth_gate_proxy", "jit_proxy", "checkout_proxy"),
    PaymentStage.CHECKOUT.value: ("checkout_proxy",),
    PaymentStage.PROMOTION.value: ("promotion_proxy", "update_proxy"),
    PaymentStage.STRIPE_INIT.value: ("stripe_init_proxy", "provider_proxy"),
    PaymentStage.PAYMENT_METHOD.value: ("payment_method_proxy", "provider_proxy"),
    PaymentStage.CONFIRM.value: ("confirm_proxy", "provider_proxy"),
    PaymentStage.APPROVE.value: ("approve_proxy", "final_review_proxy"),
    PaymentStage.REDIRECT.value: ("redirect_proxy", "provider_proxy"),
    PaymentStage.POLL.value: ("poll_proxy", "provider_proxy", "stripe_init_proxy"),
    PaymentStage.ARTIFACT.value: ("artifact_proxy", "redirect_proxy", "provider_proxy"),
}

LEGACY_POOL_KEYS: dict[str, tuple[str, ...]] = {
    PaymentStage.AUTH_GATE.value: ("auth_gate_proxy_pool", "checkout_proxy_pool"),
    PaymentStage.CHECKOUT.value: ("checkout_proxy_pool",),
    PaymentStage.PROMOTION.value: ("promotion_proxy_pool", "approve_proxy_pool"),
    PaymentStage.STRIPE_INIT.value: ("stripe_init_proxy_pool", "provider_proxy_pool", "checkout_proxy_pool"),
    PaymentStage.PAYMENT_METHOD.value: ("payment_method_proxy_pool", "provider_proxy_pool", "checkout_proxy_pool"),
    PaymentStage.CONFIRM.value: ("confirm_proxy_pool", "provider_proxy_pool", "checkout_proxy_pool"),
    PaymentStage.APPROVE.value: ("approve_proxy_pool",),
    PaymentStage.REDIRECT.value: ("redirect_proxy_pool", "provider_proxy_pool", "checkout_proxy_pool"),
    PaymentStage.POLL.value: ("poll_proxy_pool", "provider_proxy_pool", "checkout_proxy_pool"),
    PaymentStage.ARTIFACT.value: ("artifact_proxy_pool", "redirect_proxy_pool", "checkout_proxy_pool"),
}

_DEFAULT_GROUP_BY_STAGE = {
    PaymentStage.AUTH_GATE.value: "checkout",
    PaymentStage.CHECKOUT.value: "checkout",
    PaymentStage.PROMOTION.value: "approve",
    PaymentStage.STRIPE_INIT.value: "checkout",
    PaymentStage.PAYMENT_METHOD.value: "checkout",
    PaymentStage.CONFIRM.value: "checkout",
    PaymentStage.APPROVE.value: "approve",
    PaymentStage.REDIRECT.value: "checkout",
    PaymentStage.POLL.value: "checkout",
    PaymentStage.ARTIFACT.value: "checkout",
}

_ROUTE_STAGE_ALIASES = {
    "provider": PaymentStage.STRIPE_INIT.value,
    "update": PaymentStage.PROMOTION.value,
    "promotion_update": PaymentStage.PROMOTION.value,
    "taxes": PaymentStage.STRIPE_INIT.value,
    "resolve": PaymentStage.STRIPE_INIT.value,
    "custom_capability": PaymentStage.PAYMENT_METHOD.value,
    "start": PaymentStage.CONFIRM.value,
    "final_review": PaymentStage.APPROVE.value,
    "follow_redirect": PaymentStage.REDIRECT.value,
}


def route_stage(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _ROUTE_STAGE_ALIASES.get(raw) or normalize_payment_stage(raw)


def _allowed_approve_countries(payment_method: str) -> tuple[str, ...]:
    definition = PAYMENT_METHODS.get(payment_method)
    if definition is not None and definition.approve_countries:
        return tuple(definition.approve_countries)
    return ("JP", "TR") if payment_method == "gopay" else ()


def coerce_approve_country(payment_method: Any, country: Any) -> tuple[str, bool]:
    method = normalize_payment_method(payment_method, default_for_blank=False)
    value = str(country or "").strip().upper()
    if method != "gopay" or not value:
        return value, False
    allowed = _allowed_approve_countries(method)
    if not allowed or value in allowed:
        return value, False
    return ("JP" if "JP" in allowed else allowed[0]), True


def parse_proxy_pool(value: Any) -> list[str]:
    if value in (None, "", False):
        return []
    if isinstance(value, str):
        values = re.split(r"[\r\n,;]+", value)
    elif isinstance(value, Mapping):
        values = value.get("proxies") or value.get("pool") or value.get("values") or []
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]
    # Payment clients receive only canonical URLs. Invalid provider entries are
    # rejected here instead of being interpreted differently by each adapter.
    return list(dict.fromkeys(entry.url for entry in parse_proxy_list(values)))


def method_payment_config(config: Mapping[str, Any], payment_method: Any) -> dict[str, Any]:
    method = normalize_payment_method(payment_method, default_for_blank=False)
    legacy = config.get(method) if isinstance(config.get(method), Mapping) else {}
    protocol = config.get("protocol_payments") if isinstance(config.get("protocol_payments"), Mapping) else {}
    methods = protocol.get("methods") if isinstance(protocol.get("methods"), Mapping) else {}
    canonical = methods.get(method) if isinstance(methods.get(method), Mapping) else {}
    merged = {**dict(legacy), **dict(canonical)}
    routing_keys = ("stage_proxies", "stage_proxy_pools", "stage_routes", "stage_proxy_countries")
    canonical_owns_routing = any(isinstance(canonical.get(key), Mapping) for key in routing_keys)
    for key in ("stage_proxies", "stage_proxy_pools", "stage_routes"):
        old = legacy.get(key) if isinstance(legacy.get(key), Mapping) else {}
        new = canonical.get(key) if isinstance(canonical.get(key), Mapping) else {}
        if canonical_owns_routing:
            # The canonical method section owns routing maps.  Keeping old
            # entries here can silently select a proxy for a stage that the
            # canonical route intentionally leaves to its named pool.
            merged[key] = dict(new)
        elif old:
            merged[key] = dict(old)
    # Country expectations are an executable routing contract.  Once the
    # canonical protocol section declares them, do not resurrect stale values
    # from the legacy top-level section for stages it intentionally omits: the
    # planner will derive those stages from target/checkout country instead.
    legacy_countries = legacy.get("stage_proxy_countries")
    canonical_countries = canonical.get("stage_proxy_countries")
    if canonical_owns_routing:
        merged["stage_proxy_countries"] = (
            dict(canonical_countries) if isinstance(canonical_countries, Mapping) else {}
        )
    elif isinstance(legacy_countries, Mapping):
        merged["stage_proxy_countries"] = dict(legacy_countries)
    return merged


@dataclass(frozen=True)
class StageRoute:
    stage: str
    pool_name: str
    proxies: tuple[str, ...]
    expected_country: str = ""
    session_policy: str = "sticky_flow"
    failure_policy: str = "rotate_before_side_effect"


@dataclass(frozen=True)
class PaymentRoutePlan:
    payment_method: str
    flow_profile: str
    default_proxy: str
    routes: Mapping[str, StageRoute]
    selected: Mapping[str, str]
    attempts: Mapping[str, tuple[dict[str, Any], ...]] = field(default_factory=lambda: MappingProxyType({}))
    coercions: tuple[dict[str, Any], ...] = ()

    @classmethod
    def empty(cls, payment_method: Any = "") -> "PaymentRoutePlan":
        return cls(
            str(payment_method or ""),
            "unresolved",
            "",
            MappingProxyType({}),
            MappingProxyType({}),
            MappingProxyType({}),
            (),
        )

    def proxy_for(self, stage: Any, fallback: str = "") -> str:
        normalized = route_stage(stage)
        return str(self.selected.get(normalized) or fallback or self.default_proxy or "")

    @property
    def checkout_proxy(self) -> str:
        return self.proxy_for(PaymentStage.CHECKOUT)

    def to_adapter_options(self) -> dict[str, Any]:
        selected = dict(self.selected)
        options = {
            "payment_route_plan": self,
            "stage_proxy_countries": {
                stage: route.expected_country
                for stage, route in self.routes.items()
                if route.expected_country
            },
            "stage_proxies": selected,
        }
        key_map = {
            "auth_gate": "auth_gate_proxy",
            "checkout": "checkout_proxy",
            "promotion": "promotion_proxy",
            "stripe_init": "stripe_init_proxy",
            "payment_method": "payment_method_proxy",
            "confirm": "confirm_proxy",
            "approve": "approve_proxy",
            "redirect": "redirect_proxy",
            "poll": "poll_proxy",
            "artifact": "artifact_proxy",
        }
        for stage, key in key_map.items():
            if selected.get(stage):
                options[key] = selected[stage]
        if selected.get("stripe_init"):
            options["provider_proxy"] = selected["stripe_init"]
        if selected.get("promotion"):
            options["update_proxy"] = selected["promotion"]
        if selected.get("approve"):
            options["final_review_proxy"] = selected["approve"]
        return options

    def public_dict(self) -> dict[str, Any]:
        from .paypal_proxy import redact_proxy_url

        return {
            "payment_method": self.payment_method,
            "flow_profile": self.flow_profile,
            "default_proxy_present": bool(self.default_proxy),
            "stages": {
                stage: {
                    "pool": route.pool_name,
                    "pool_size": len(route.proxies),
                    "expected_country": route.expected_country,
                    "proxy": redact_proxy_url(self.selected.get(stage, "")),
                    "session_policy": route.session_policy,
                    "failure_policy": route.failure_policy,
                }
                for stage, route in self.routes.items()
            },
        }


class PaymentRoutePlanner:
    def __init__(self, config: Mapping[str, Any], *, proxy_state: Any = None) -> None:
        self.config = config
        self.protocol = config.get("protocol_payments") if isinstance(config.get("protocol_payments"), Mapping) else {}
        if proxy_state is None:
            from .paypal_proxy import proxy_state_from_config

            proxy_state = proxy_state_from_config(config)
        self.proxy_state = proxy_state

    def plan(
        self,
        payment_method: Any,
        *,
        options: Mapping[str, Any] | None = None,
        default_proxy: Any = None,
        pool_offset: int = 0,
        select_proxies: bool = True,
    ) -> PaymentRoutePlan:
        method = normalize_payment_method(payment_method, default_for_blank=False)
        if not method:
            raise ValueError(f"unsupported payment method: {payment_method}")
        values = dict(options or {})
        automatic_country = bool(values.get("auto_proxy_country"))
        method_cfg = method_payment_config(self.config, method)
        profile = payment_flow_profile(method, method_cfg)
        shared_override = self._proxy_value(values.get("proxy"))
        default = self._proxy_value(
            default_proxy
            or values.get("proxy")
        )
        countries = self._countries(method, method_cfg, values)
        self._validate_countries(method, countries, values)
        raw_explicit_countries = (
            values.get("stage_proxy_countries")
            if not automatic_country and isinstance(values.get("stage_proxy_countries"), Mapping)
            else {}
        )
        explicit_countries = {
            normalize_payment_stage(key): str(value or "").strip().upper()
            for key, value in raw_explicit_countries.items()
            if normalize_payment_stage(key) and str(value or "").strip()
        }
        coercions: list[dict[str, Any]] = []
        if method == "gopay":
            original = countries.get("approve") or "JP"
            countries["approve"], changed = coerce_approve_country(method, original)
            if changed:
                coercions.append({"field": "approve_country", "original": original, "coerced": countries["approve"]})
        injected_transport = values.get("transport") is not None
        ignore_configured_routes = injected_transport or bool(shared_override)
        routing_method_cfg = {} if ignore_configured_routes else method_cfg
        named_pools = {} if ignore_configured_routes else self._named_pools()
        groups = self._legacy_groups(
            routing_method_cfg,
            values,
            default,
        )
        stage_routes = routing_method_cfg.get("stage_routes") if isinstance(routing_method_cfg.get("stage_routes"), Mapping) else {}
        explicit_stage_routes = values.get("stage_routes") if isinstance(values.get("stage_routes"), Mapping) else {}
        stage_routes = {**dict(stage_routes), **dict(explicit_stage_routes)}
        stage_pools = routing_method_cfg.get("stage_proxy_pools") if isinstance(routing_method_cfg.get("stage_proxy_pools"), Mapping) else {}
        explicit_stage_pools = values.get("stage_proxy_pools") if isinstance(values.get("stage_proxy_pools"), Mapping) else {}
        stage_pools = {**dict(stage_pools), **dict(explicit_stage_pools)}
        configured_stage_proxies = routing_method_cfg.get("stage_proxies") if isinstance(routing_method_cfg.get("stage_proxies"), Mapping) else {}
        explicit_stage_proxies = values.get("stage_proxies") if isinstance(values.get("stage_proxies"), Mapping) else {}
        checkout_scalar = self._explicit_stage_proxy(
            PaymentStage.CHECKOUT.value,
            explicit_stage_proxies,
            values,
            routing_method_cfg,
        )
        approve_scalar = self._explicit_stage_proxy(
            PaymentStage.APPROVE.value,
            explicit_stage_proxies,
            values,
            routing_method_cfg,
        ) or checkout_scalar
        scalar_groups = {"checkout": checkout_scalar, "approve": approve_scalar}

        routes: dict[str, StageRoute] = {}
        selected: dict[str, str] = {}
        attempts: dict[str, tuple[dict[str, Any], ...]] = {}
        selected_by_group: dict[tuple[str, str], str] = {}
        rotated_sessions: dict[tuple[str, str], str] = {}

        for stage in profile.stages:
            if stage == PaymentStage.ARTIFACT.value:
                continue
            route_cfg = stage_routes.get(stage)
            route_cfg = route_cfg if isinstance(route_cfg, Mapping) else {"pool": route_cfg} if route_cfg else {}
            group_name = str(route_cfg.get("pool") or _DEFAULT_GROUP_BY_STAGE.get(stage) or "checkout").strip()
            explicit = self._explicit_stage_proxy(stage, explicit_stage_proxies, values, {})
            explicit_pool = self._first_pool(values, keys=LEGACY_POOL_KEYS.get(stage, ()))
            if explicit_pool:
                pool = explicit_pool
                group_name = _DEFAULT_GROUP_BY_STAGE.get(stage, group_name)
            elif explicit:
                pool = [explicit]
                group_name = f"explicit:{stage}"
            else:
                pool = parse_proxy_pool(route_cfg.get("proxies"))
                if not pool:
                    pool = parse_proxy_pool(stage_pools.get(stage))
                if not pool and group_name in named_pools:
                    pool = list(named_pools[group_name])
            if not pool:
                pool = list(groups.get(group_name) or ())
            if not pool:
                configured = self._explicit_stage_proxy(
                    stage,
                    configured_stage_proxies,
                    {},
                    routing_method_cfg,
                )
                if configured:
                    pool = [configured]
                    group_name = f"configured:{stage}"
            if not pool and default:
                pool = [default]
                group_name = "default"
            expected = str(
                explicit_countries.get(stage)
                or ("" if automatic_country else route_cfg.get("country"))
                or countries.get(stage)
                or ""
            ).strip().upper()
            session_policy = str(route_cfg.get("session_policy") or "sticky_flow").strip()
            failure_policy = str(route_cfg.get("failure_policy") or "rotate_before_side_effect").strip()
            route = StageRoute(stage, group_name, tuple(pool), expected, session_policy, failure_policy)
            routes[stage] = route
            if not pool:
                selected[stage] = ""
                continue
            selection_stage = group_name if group_name in {"checkout", "approve"} else stage
            selection_expected = (
                countries.get(selection_stage, expected)
                if selection_stage in {"checkout", "approve"}
                else expected
            )
            reuse_key = (
                group_name,
                selection_expected if session_policy == "sticky_country" else "",
            )
            chosen_base = selected_by_group.get(reuse_key, "")
            stage_attempts: list[dict[str, Any]] = []
            scalar_group = False
            if not chosen_base:
                ordered = list(pool)
                if ordered and pool_offset:
                    offset = int(pool_offset) % len(ordered)
                    ordered = ordered[offset:] + ordered[:offset]
                explicit_seed = bool(values.get("proxy") or values.get("checkout_proxy"))
                scalar_group = (
                    len(ordered) == 1
                    and bool(scalar_groups.get(group_name))
                    and self._proxy_value(ordered[0]) == scalar_groups[group_name]
                )
                should_probe = bool(ordered) and (
                    select_proxies
                    and not group_name.startswith("explicit:")
                    and not group_name.startswith("configured:")
                    and not scalar_group
                    and not (
                        explicit_seed
                        and len(ordered) == 1
                        and self._proxy_value(ordered[0]) == default
                    )
                )
                if should_probe:
                    from .paypal_proxy import select_proxy_from_pool

                    chosen_base, stage_attempts = select_proxy_from_pool(
                        ordered,
                        selection_expected,
                        selection_stage,
                        state=self.proxy_state,
                    )
                    if not chosen_base:
                        raise RuntimeError(f"payment_{selection_stage}_proxy_pool_unavailable")
                elif ordered:
                    chosen_base = ordered[0]
                if chosen_base:
                    selected_by_group[reuse_key] = chosen_base
            chosen = chosen_base
            configured_countries = routing_method_cfg.get("stage_proxy_countries") if isinstance(routing_method_cfg.get("stage_proxy_countries"), Mapping) else {}
            explicit_countries = values.get("stage_proxy_countries") if isinstance(values.get("stage_proxy_countries"), Mapping) else {}
            country_requested = bool(
                route_cfg.get("country")
                or configured_countries.get(stage)
                or explicit_countries.get(stage)
                or (stage in {PaymentStage.AUTH_GATE.value, PaymentStage.CHECKOUT.value} and (values.get("checkout_country") or values.get("target_country")))
                or (stage == PaymentStage.APPROVE.value and values.get("approve_country"))
            )
            scalar_route = bool(explicit) or scalar_group or group_name.startswith("configured:") or bool(shared_override)
            if chosen and expected and (country_requested or not scalar_route):
                from .paypal_proxy import rotate_proxy_session
                rotation_key = (chosen, expected)
                chosen = rotated_sessions.get(rotation_key)
                if not chosen:
                    chosen = rotate_proxy_session(chosen_base, expected)
                    rotated_sessions[rotation_key] = chosen
            selected[stage] = chosen
            attempts[stage] = tuple(stage_attempts)

        for stage in STAGE_ORDER:
            if stage not in routes:
                continue
            if not selected.get(stage):
                fallback_stage = _DEFAULT_GROUP_BY_STAGE.get(stage, "checkout")
                selected[stage] = selected.get(fallback_stage, "") or default
        return PaymentRoutePlan(
            method,
            profile.key,
            selected.get(PaymentStage.CHECKOUT.value, "") or default,
            MappingProxyType(routes),
            MappingProxyType(selected),
            MappingProxyType(attempts),
            tuple(coercions),
        )

    def _named_pools(self) -> dict[str, tuple[str, ...]]:
        raw = self.protocol.get("proxy_pools") if isinstance(self.protocol.get("proxy_pools"), Mapping) else {}
        return {str(name): tuple(parse_proxy_pool(value)) for name, value in raw.items()}

    def _legacy_groups(self, method_cfg: Mapping[str, Any], values: Mapping[str, Any], default: str) -> dict[str, tuple[str, ...]]:
        protocol_fallback = (
            parse_proxy_pool(self.protocol.get("proxy_pool"))
            if values.get("use_protocol_proxy_pool")
            else []
        )
        explicit_checkout = self._first_proxy_pool(values, keys=LEGACY_STAGE_KEYS["checkout"])
        explicit_approve = self._first_proxy_pool(values, keys=LEGACY_STAGE_KEYS["approve"])
        checkout = self._first_pool(
            values, method_cfg, keys=LEGACY_POOL_KEYS["checkout"]
        ) or explicit_checkout
        approve = self._first_pool(
            values, method_cfg, keys=LEGACY_POOL_KEYS["approve"]
        ) or explicit_approve
        if not checkout:
            checkout = self._first_proxy_pool(values, method_cfg, keys=LEGACY_STAGE_KEYS["checkout"])
        if not approve:
            approve = (
                explicit_checkout
                or self._first_proxy_pool(method_cfg, keys=LEGACY_STAGE_KEYS["approve"])
            )
        fallback = tuple(protocol_fallback or ([default] if default else []))
        return {
            "checkout": tuple(checkout) or fallback,
            "approve": tuple(approve) or tuple(checkout) or fallback,
            "default": fallback,
        }

    def _countries(self, method: str, method_cfg: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, str]:
        automatic = bool(values.get("auto_proxy_country"))
        configured = {} if automatic else (method_cfg.get("stage_proxy_countries") if isinstance(method_cfg.get("stage_proxy_countries"), Mapping) else {})
        explicit = {} if automatic else (values.get("stage_proxy_countries") if isinstance(values.get("stage_proxy_countries"), Mapping) else {})
        countries = {
            normalize_payment_stage(key): str(value or "").strip().upper()
            for key, value in {**dict(configured), **dict(explicit)}.items()
            if normalize_payment_stage(key) and str(value or "").strip()
        }
        spec = PAYMENT_METHODS[method]
        target = str(values.get("target_country") or values.get("checkout_country") or spec.country).strip().upper()
        countries.setdefault(PaymentStage.AUTH_GATE.value, countries.get(PaymentStage.CHECKOUT.value, target))
        countries.setdefault(PaymentStage.CHECKOUT.value, target)
        countries.setdefault(PaymentStage.STRIPE_INIT.value, countries.get("provider", target))
        countries.setdefault(PaymentStage.PAYMENT_METHOD.value, countries.get(PaymentStage.STRIPE_INIT.value, target))
        countries.setdefault(PaymentStage.CONFIRM.value, countries.get(PaymentStage.STRIPE_INIT.value, target))
        countries.setdefault(PaymentStage.REDIRECT.value, countries.get(PaymentStage.STRIPE_INIT.value, target))
        countries.setdefault(PaymentStage.POLL.value, countries.get(PaymentStage.STRIPE_INIT.value, target))
        approve = str(values.get("approve_country") or "").strip().upper()
        countries.setdefault(
            PaymentStage.APPROVE.value,
            approve or ("JP" if method == "gopay" else target),
        )
        if method == "gopay":
            countries.setdefault(PaymentStage.PROMOTION.value, "TH")
        else:
            countries.setdefault(PaymentStage.PROMOTION.value, countries.get(PaymentStage.APPROVE.value, target))
        return countries

    @staticmethod
    def _validate_countries(method: str, countries: Mapping[str, str], values: Mapping[str, Any]) -> None:
        invalid = {
            stage: country
            for stage, country in countries.items()
            if country and not re.fullmatch(r"[A-Z]{2}", country)
        }
        if invalid:
            rendered = ", ".join(f"{stage}={country}" for stage, country in sorted(invalid.items()))
            raise ValueError(f"invalid payment route country: {rendered}")
        if method != "paypal":
            return

    @staticmethod
    def _proxy_value(value: Any) -> str:
        if isinstance(value, Mapping):
            value = value.get("https") or value.get("http") or ""
        raw = str(value or "").strip()
        if not raw:
            return ""
        return resolve_proxy_value(raw) or raw

    def _explicit_stage_proxy(
        self,
        stage: str,
        stage_proxies: Mapping[str, Any],
        values: Mapping[str, Any],
        method_cfg: Mapping[str, Any],
    ) -> str:
        candidates = [stage_proxies.get(stage)]
        for key in LEGACY_STAGE_KEYS.get(stage, ()):
            candidates.extend((values.get(key), method_cfg.get(key)))
        for value in candidates:
            resolved = self._proxy_value(value)
            if resolved:
                return resolved
        return ""

    @staticmethod
    def _first_pool(*sources: Mapping[str, Any], keys: tuple[str, ...]) -> list[str]:
        for source in sources:
            for key in keys:
                pool = parse_proxy_pool(source.get(key))
                if pool:
                    return pool
        return []

    @staticmethod
    def _first_proxy_pool(*sources: Mapping[str, Any], keys: tuple[str, ...]) -> list[str]:
        for source in sources:
            for key in keys:
                value = source.get(key)
                if value:
                    return [str(value).strip()]
        return []


def payment_proxy_pools(config: Mapping[str, Any], payment_method: Any) -> dict[str, list[str]]:
    method_cfg = method_payment_config(config, payment_method)
    protocol = config.get("protocol_payments") if isinstance(config.get("protocol_payments"), Mapping) else {}
    named = protocol.get("proxy_pools") if isinstance(protocol.get("proxy_pools"), Mapping) else {}
    routes = method_cfg.get("stage_routes") if isinstance(method_cfg.get("stage_routes"), Mapping) else {}

    def pool_for(stage: str, legacy_key: str) -> list[str]:
        configured = routes.get(stage)
        if isinstance(configured, Mapping):
            direct = parse_proxy_pool(configured.get("proxies"))
            pool_name = str(configured.get("pool") or "").strip()
        else:
            direct = []
            pool_name = str(configured or "").strip()
        return direct or parse_proxy_pool(named.get(pool_name)) or PaymentRoutePlanner._first_pool(
            method_cfg, keys=LEGACY_POOL_KEYS[legacy_key]
        )

    return {
        "checkout": pool_for(PaymentStage.CHECKOUT.value, "checkout"),
        "approve": pool_for(PaymentStage.APPROVE.value, "approve"),
    }


__all__ = [
    "LEGACY_POOL_KEYS",
    "LEGACY_STAGE_KEYS",
    "PaymentRoutePlan",
    "PaymentRoutePlanner",
    "StageRoute",
    "coerce_approve_country",
    "method_payment_config",
    "parse_proxy_pool",
    "payment_proxy_pools",
    "route_stage",
]
