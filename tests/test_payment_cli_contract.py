from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sms_tool import account_creation, cli, registration


def test_registration_has_no_payment_generation_entrypoint():
    assert not hasattr(registration, "_pipeline_payment_link")
    assert not hasattr(registration, "_generate_payment_link")
    assert not hasattr(account_creation, "_generate_payment_link")


def test_qr_only_registration_session_is_marked_ready():
    session = registration._build_session_file({
        "email": "qr@example.com",
        "access_token": "at-test",
        "paypal": {"ok": True, "payment_method": "momo", "qr_path": "qr.png"},
    })
    assert session["paypal_status"] == "qr_ready"


def test_blik_batch_requires_the_single_account_command():
    args = SimpleNamespace(
        payment_method="blik",
        email_file="accounts.txt",
        payment_probe_only=False,
    )
    with pytest.raises(SystemExit) as exc:
        cli._extract_payment_link(args)
    assert exc.value.code == 2


def test_single_account_probe_runs_checkout_capability_instead_of_stopping_after_auth():
    args = SimpleNamespace(
        payment_method="gopay",
        email_file="",
        email="probe@example.com",
        session_file="",
        at=None,
        proxy=None,
        proxy_explicit=False,
        refresh_timeout=30,
        no_jit_at_refresh=False,
        payment_probe_only=True,
        desktop_ipc=False,
    )
    auth = {
        "ok": True,
        "access_token": "secret-at",
        "auth_context": {"email": "probe@example.com"},
    }
    capability = {
        "ok": True,
        "operation": "payment_method_capability_probe",
        "classification": "eligible",
        "eligible": True,
    }

    with patch.object(cli, "CFG", {}), \
         patch.object(cli, "_resolve_payment_access_token", return_value=("", None)), \
         patch.object(cli, "_protocol_proxy_pool", return_value=[]), \
         patch("sms_tool.payment_auth.ensure_payment_access_token", return_value=auth), \
         patch("sms_tool.payment_link_manager.generate_payment_link", return_value=capability) as generate:
        cli._extract_payment_link(args)

    assert generate.call_count == 1
    assert generate.call_args.kwargs["access_token"] == "secret-at"
    assert generate.call_args.kwargs["payment_method"] == "gopay"
    assert generate.call_args.kwargs["probe_only"] is True


def test_probe_batch_returns_nonzero_when_capability_is_unknown(tmp_path):
    email_file = tmp_path / "accounts.txt"
    email_file.write_text("probe@example.com\n", encoding="utf-8")
    args = SimpleNamespace(
        payment_method="gopay",
        email_file=str(email_file),
        email=None,
        payment_probe_only=True,
        desktop_ipc=False,
        proxy=None,
        proxy_explicit=False,
    )
    report = {
        "ok": False,
        "counts": {"authenticated": 1, "capability_probed": 1},
        "results": [{
            "classification": "unknown",
            "error_code": "stripe_init_failed",
        }],
    }

    with patch.object(cli, "CFG", {}), \
         patch.object(cli, "_protocol_proxy_pool", return_value=[]), \
         patch("sms_tool.payment_batch.run_payment_batch", return_value=report):
        with pytest.raises(SystemExit) as exc:
            cli._extract_payment_link(args)

    assert exc.value.code == 3


def test_payment_stage_args_preserves_legacy_country_override_hook():
    args = SimpleNamespace(
        proxy="http://seed.example:8080",
        proxy_explicit=True,
        checkout_proxy_country="PH",
        approve_proxy_country="",
    )
    expected = ("legacy", "checkout", "provider", "approve")

    with patch.object(cli, "CFG", {}), patch.object(
        cli, "_apply_stage_country_overrides", return_value=expected
    ) as apply:
        result = cli._at_payment_stage_args(args, "gopay")

    assert result == expected
    apply.assert_called_once()


def test_payment_proxy_pool_parser_accepts_comma_and_newline_values():
    assert cli.payment_commands.parse_proxy_pool(
        " http://checkout-a:8080\nhttp://checkout-b:8080, http://checkout-a:8080 "
    ) == ["http://checkout-a:8080", "http://checkout-b:8080"]


def test_payment_route_selects_checkout_and_approve_pools_independently():
    args = SimpleNamespace(
        proxy=None,
        proxy_explicit=False,
        checkout_proxy="http://legacy-checkout:8080",
        approve_proxy="http://legacy-approve:8080",
        checkout_proxy_pool="http://checkout-a:8080,http://checkout-b:8080",
        approve_proxy_pool="http://approve-a:8080\nhttp://approve-b:8080",
        provider_proxy=None,
        promotion_proxy=None,
        target_country="US",
        checkout_country="JP",
    )
    context = cli.payment_commands.PaymentCommandContext(
        read_email_file=lambda _: [],
        payment_method=lambda _: "gopay",
        resolve_access_token=lambda _: ("", None),
        payment_stage_args=lambda *_: (
            None,
            args.checkout_proxy,
            None,
            args.approve_proxy,
        ),
        promotion_proxy_arg=lambda *_: None,
        stage_country_overrides=lambda *_: {"checkout": "JP", "approve": "TR"},
        payment_country=lambda *_: "US",
        protocol_proxy_pool=lambda: [],
        has_explicit_payment_proxy=lambda _: False,
        payment_proxy_pools=lambda _: {"checkout": [], "approve": []},
    )
    seen = []

    def choose(pool, expected_country, stage, **_kwargs):
        seen.append((list(pool), expected_country, stage))
        return (f"http://selected-{stage}:8080", [{"ok": True, "stage": stage}])

    with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
        route = cli.payment_commands.resolve_payment_route(args, "gopay", context)

    assert route["checkout_proxy"] == "http://selected-checkout:8080"
    assert route["approve_proxy"] == "http://selected-approve:8080"
    assert route["proxy"] == route["checkout_proxy"]
    assert [item[2] for item in seen] == ["checkout", "approve"]
    assert seen[0][1] == "JP"
    assert seen[1][1] == "TR"
    assert route["checkout_proxy_pool"] == [
        "http://checkout-a:8080", "http://checkout-b:8080"
    ]
    assert route["approve_proxy_pool"] == [
        "http://approve-a:8080", "http://approve-b:8080"
    ]


def test_gopay_route_defaults_approve_pool_country_to_jp():
    args = SimpleNamespace(
        proxy=None,
        proxy_explicit=False,
        checkout_proxy=None,
        approve_proxy=None,
        checkout_proxy_pool="http://checkout.example:8080",
        approve_proxy_pool="http://approve.example:8080",
        provider_proxy=None,
        promotion_proxy=None,
        target_country="ID",
        checkout_country="ID",
    )
    context = cli.payment_commands.PaymentCommandContext(
        read_email_file=lambda _: [],
        payment_method=lambda _: "gopay",
        resolve_access_token=lambda _: ("", None),
        payment_stage_args=lambda *_: (None, None, None, None),
        promotion_proxy_arg=lambda *_: None,
        stage_country_overrides=lambda *_: {},
        payment_country=lambda *_: "ID",
        protocol_proxy_pool=lambda: [],
        has_explicit_payment_proxy=lambda _: False,
        payment_proxy_pools=lambda _: {"checkout": [], "approve": []},
    )
    seen = []

    def choose(pool, expected_country, stage, **_kwargs):
        seen.append((list(pool), expected_country, stage))
        return pool[0], [{"ok": True}]

    with patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=choose):
        route = cli.payment_commands.resolve_payment_route(args, "gopay", context)

    assert route["checkout_proxy"] == "http://checkout.example:8080"
    assert route["approve_proxy"] == "http://approve.example:8080"
    assert [item[1] for item in seen] == ["ID", "JP"]


def test_gopay_shared_pool_fallback_defaults_approve_country_to_jp():
    args = SimpleNamespace(
        proxy=None,
        proxy_explicit=False,
        checkout_proxy=None,
        approve_proxy=None,
        checkout_proxy_pool=None,
        approve_proxy_pool=None,
        provider_proxy=None,
        promotion_proxy=None,
        target_country="ID",
        checkout_country="ID",
    )
    context = cli.payment_commands.PaymentCommandContext(
        read_email_file=lambda _: [],
        payment_method=lambda _: "gopay",
        resolve_access_token=lambda _: ("", None),
        payment_stage_args=lambda *_: (None, None, None, None),
        promotion_proxy_arg=lambda *_: None,
        stage_country_overrides=lambda *_: {},
        payment_country=lambda *_: "ID",
        protocol_proxy_pool=lambda: ["http://pool.example:8080"],
        has_explicit_payment_proxy=lambda _: False,
        payment_proxy_pools=lambda _: {"checkout": [], "approve": []},
    )
    rotations = []

    def rotate(proxy, country):
        rotations.append((proxy, country))
        return f"{proxy}/{country}"

    with patch(
        "sms_tool.paypal_proxy.select_proxy_from_pool",
        return_value=("http://pool.example:8080", [{"ok": True}]),
    ), patch("sms_tool.paypal_proxy.rotate_proxy_session", side_effect=rotate):
        route = cli.payment_commands.resolve_payment_route(args, "gopay", context)

    assert route["approve_proxy"].endswith("/JP")
    assert ("http://pool.example:8080", "JP") in rotations


def test_payment_stage_args_are_owned_by_protocol_method_config():
    args = SimpleNamespace(
        proxy="http://registration.example:8080",
        proxy_explicit=False,
        checkout_proxy=None,
        provider_proxy=None,
        approve_proxy=None,
        checkout_proxy_country="",
        approve_proxy_country="",
    )
    config = {
        "proxy": {"default": "http://registration.example:8080"},
        "paypal": {
            "stage_proxies": {
                "checkout": "http://paypal-checkout.example:8080",
                "provider": "http://paypal-provider.example:8080",
                "approve": "http://paypal-approve.example:8080",
            },
        },
        "protocol_payments": {
            "methods": {
                "gopay": {
                    "proxy": "http://gopay-base.example:8080",
                    "stage_proxies": {
                        "checkout": "http://gopay-checkout.example:8080",
                        "provider": "http://gopay-provider.example:8080",
                        "approve": "http://gopay-approve.example:8080",
                    },
                },
            },
        },
    }

    result = cli.payment_commands.payment_stage_args(args, "gopay", config)

    assert result == (
        "http://gopay-base.example:8080",
        "http://gopay-checkout.example:8080",
        "http://gopay-provider.example:8080",
        "http://gopay-approve.example:8080",
    )


def test_non_paypal_route_does_not_inherit_registration_or_paypal_proxies():
    args = SimpleNamespace(
        proxy="http://registration.example:8080",
        proxy_explicit=False,
        checkout_proxy=None,
        provider_proxy=None,
        approve_proxy=None,
        promotion_proxy=None,
        checkout_proxy_country="",
        approve_proxy_country="",
        promotion_proxy_country="",
    )
    config = {
        "proxy": {"default": "http://registration.example:8080"},
        "paypal": {
            "stage_proxies": {
                "checkout": "http://paypal-checkout.example:8080",
                "provider": "http://paypal-provider.example:8080",
                "approve": "http://paypal-approve.example:8080",
                "promotion": "http://paypal-promotion.example:8080",
            },
        },
        "protocol_payments": {"methods": {"gopay": {}}},
    }

    assert cli.payment_commands.payment_stage_args(args, "gopay", config) == (
        None,
        None,
        None,
        None,
    )
    assert cli.payment_commands.promotion_proxy_arg(args, "gopay", config) is None


def test_promotion_proxy_comes_from_protocol_method_config():
    args = SimpleNamespace(promotion_proxy=None, promotion_proxy_country="")
    config = {
        "paypal": {"stage_proxies": {"promotion": "http://paypal.example:8080"}},
        "protocol_payments": {
            "methods": {
                "gopay": {
                    "stage_proxies": {"promotion_update": "http://gopay-update.example:8080"},
                },
            },
        },
    }

    assert cli.payment_commands.promotion_proxy_arg(args, "gopay", config) == (
        "http://gopay-update.example:8080"
    )


def test_gopay_stage_country_defaults_to_th_and_config_and_cli_take_precedence():
    args = SimpleNamespace(
        checkout_proxy_country="",
        approve_proxy_country="",
        promotion_proxy_country="",
    )
    config = {
        "protocol_payments": {
            "methods": {
                "gopay": {
                    "stage_proxy_countries": {
                        "provider": "ID",
                        "promotion_update": "VN",
                    },
                },
            },
        },
    }

    assert cli.payment_commands.stage_country_overrides(args, "gopay", {}) == {
        "promotion": "TH",
    }
    assert cli.payment_commands.stage_country_overrides(args, "gopay", config) == {
        "provider": "ID",
        "promotion": "VN",
    }

    args.promotion_proxy_country = "PH"
    assert cli.payment_commands.stage_country_overrides(args, "gopay", config)[
        "promotion"
    ] == "PH"


def test_explicit_proxy_resolver_failure_is_not_swallowed():
    args = SimpleNamespace(
        proxy="http://operator.example:8080",
        proxy_explicit=True,
        checkout_proxy=None,
        provider_proxy=None,
        approve_proxy=None,
    )

    with patch(
        "sms_tool.proxy_entry.resolve_proxy_value",
        side_effect=RuntimeError("resolver unavailable"),
    ):
        with pytest.raises(RuntimeError, match="resolver unavailable"):
            cli.payment_commands.payment_stage_args(args, "gopay", {})


def test_invalid_explicit_proxy_fails_closed():
    args = SimpleNamespace(
        proxy="invalid-proxy-without-port",
        proxy_explicit=True,
        checkout_proxy=None,
        provider_proxy=None,
        approve_proxy=None,
    )

    with pytest.raises(ValueError, match="invalid --proxy value"):
        cli.payment_commands.payment_stage_args(args, "gopay", {})


def test_single_payment_resolves_checkout_route_before_jit_and_reuses_it():
    args = SimpleNamespace(
        payment_method="gopay",
        email_file="",
        email="route@example.com",
        session_file="",
        at=None,
        proxy=None,
        proxy_explicit=False,
        refresh_timeout=30,
        no_jit_at_refresh=False,
        payment_probe_only=True,
        desktop_ipc=False,
    )
    config = {
        "protocol_payments": {
            "methods": {
                "gopay": {
                    "stage_proxies": {
                        "checkout": "http://checkout.example:8080",
                        "provider": "http://provider.example:8080",
                    },
                },
            },
        },
    }
    auth_result = {
        "ok": True,
        "access_token": "secret-at",
        "auth_context": {"email": "route@example.com"},
    }

    with patch.object(cli, "CFG", config), \
         patch.object(cli, "_resolve_payment_access_token", return_value=("", None)), \
         patch("sms_tool.payment_auth.ensure_payment_access_token", return_value=auth_result) as auth, \
         patch(
             "sms_tool.payment_link_manager.generate_payment_link",
             return_value={"ok": True, "operation": "probe"},
         ) as generate:
        cli._extract_payment_link(args)

    checkout_route = "http://checkout.example:8080"
    assert auth.call_args.kwargs["proxy"] == checkout_route
    assert generate.call_args.kwargs["proxy"] == checkout_route
    assert generate.call_args.kwargs["checkout_proxy"] == checkout_route


def test_healthy_pool_is_selected_before_jit_and_shared_with_checkout():
    events = []
    rotations = []
    args = SimpleNamespace(
        payment_method="gopay",
        email_file="",
        email="pool@example.com",
        session_file="",
        at=None,
        proxy=None,
        proxy_explicit=False,
        promotion_proxy="http://promotion.example:8080",
        refresh_timeout=30,
        no_jit_at_refresh=False,
        payment_probe_only=True,
        desktop_ipc=False,
    )
    config = {
        "protocol_payments": {
            "proxy_pool": ["http://pool.example:8080"],
            "methods": {
                "gopay": {
                    "stage_proxies": {"provider": "http://provider.example:8080"},
                },
            },
        },
    }

    def select_proxy(*_args, **_kwargs):
        events.append("select")
        return "http://healthy.example:8080", []

    def ensure_auth(**_kwargs):
        events.append("auth")
        return {
            "ok": True,
            "access_token": "secret-at",
            "auth_context": {"email": "pool@example.com"},
        }

    def rotate_proxy(value, country):
        rotations.append((value, country))
        return value

    with patch.object(cli, "CFG", config), \
         patch.object(cli, "_resolve_payment_access_token", return_value=("", None)), \
         patch("sms_tool.paypal_proxy.select_proxy_from_pool", side_effect=select_proxy), \
         patch("sms_tool.paypal_proxy.rotate_proxy_session", side_effect=rotate_proxy), \
         patch("sms_tool.payment_auth.ensure_payment_access_token", side_effect=ensure_auth) as auth, \
         patch(
             "sms_tool.payment_link_manager.generate_payment_link",
             return_value={"ok": True, "operation": "probe"},
         ) as generate:
        cli._extract_payment_link(args)

    assert events == ["select", "auth"]
    assert auth.call_args.kwargs["proxy"] == "http://healthy.example:8080"
    assert generate.call_args.kwargs["proxy"] == "http://healthy.example:8080"
    assert generate.call_args.kwargs["checkout_proxy"] == "http://healthy.example:8080"
    assert generate.call_args.kwargs["provider_proxy"] == "http://provider.example:8080"
    assert generate.call_args.kwargs["promotion_proxy"] == "http://promotion.example:8080"
    assert ("http://promotion.example:8080", "TH") in rotations


def test_batch_jit_receives_the_resolved_checkout_route(tmp_path):
    email_file = tmp_path / "accounts.txt"
    email_file.write_text("batch@example.com\n", encoding="utf-8")
    args = SimpleNamespace(
        payment_method="gopay",
        email_file=str(email_file),
        email=None,
        proxy=None,
        proxy_explicit=False,
        payment_probe_only=True,
        desktop_ipc=False,
    )
    config = {
        "protocol_payments": {
            "methods": {
                "gopay": {
                    "stage_proxies": {"checkout": "http://checkout.example:8080"},
                },
            },
        },
    }
    report = {"ok": True, "counts": {"capability_probed": 1}, "results": []}

    with patch.object(cli, "CFG", config), \
         patch("sms_tool.payment_batch.run_payment_batch", return_value=report) as batch:
        cli._extract_payment_link(args)

    assert batch.call_args.kwargs["proxy"] == "http://checkout.example:8080"
    assert batch.call_args.kwargs["payment_kwargs"]["checkout_proxy"] == (
        "http://checkout.example:8080"
    )


def test_cli_payment_method_choices_follow_catalog_and_accept_alias_and_th(monkeypatch):
    from sms_tool.payment_catalog import PAYMENT_CATALOG

    assert set(cli._payment_method_choices()) == set(PAYMENT_CATALOG.aliases)
    monkeypatch.setattr(
        "sys.argv",
        [
            "chatgpt_phone_reg.py",
            "--extract-payment-link",
            "--at",
            "test-at",
            "--payment-method",
            "go-pay",
            "--update-proxy-country",
            "TH",
        ],
    )
    with patch.object(cli, "CFG", {}), patch(
        "sms_tool.payment_link_manager.generate_payment_link",
        return_value={"ok": True, "operation": "probe"},
    ) as generate:
        cli.main()

    assert generate.call_args.kwargs["payment_method"] == "gopay"
    assert generate.call_args.kwargs["stage_proxy_countries"]["promotion"] == "TH"


def _regenerate_args(**overrides):
    values = {
        "email": "regen@example.com",
        "email_file": "",
        "session_file": "",
        "payment_method": "gopay",
        "proxy": "http://registration.example:8080",
        "proxy_explicit": False,
        "checkout_proxy": None,
        "provider_proxy": None,
        "approve_proxy": None,
        "promotion_proxy": None,
        "checkout_proxy_country": "",
        "approve_proxy_country": "",
        "promotion_proxy_country": "",
        "target_country": "",
        "checkout_country": "",
        "paypal_generation_type": None,
        "no_require_zero": False,
        "workers": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _gopay_route_config():
    return {
        "proxy": {"default": "http://registration.example:8080"},
        "paypal": {
            "stage_proxies": {"checkout": "http://paypal.example:8080"},
        },
        "protocol_payments": {
            "methods": {
                "gopay": {
                    "stage_proxies": {
                        "checkout": "http://checkout.example:8080",
                        "provider": "http://provider.example:8080",
                        "approve": "http://approve.example:8080",
                        "promotion": "http://promotion.example:8080",
                    },
                    "stage_proxy_countries": {"promotion": "TH"},
                },
            },
        },
    }


def _assert_regenerate_route(call):
    assert call.kwargs["proxy"] == "http://checkout.example:8080"
    assert call.kwargs["checkout_proxy"] == "http://checkout.example:8080"
    assert call.kwargs["provider_proxy"] == "http://provider.example:8080"
    assert call.kwargs["approve_proxy"] == "http://approve.example:8080"
    assert call.kwargs["promotion_proxy"] == "http://promotion.example:8080"
    assert call.kwargs["require_zero"] is True


def test_single_regenerate_uses_method_owned_payment_route():
    args = _regenerate_args()

    with patch.object(cli, "CFG", _gopay_route_config()), patch(
        "sms_tool.paypal_links.regenerate_paypal_link",
        return_value={"ok": True},
    ) as regenerate:
        cli._regenerate_paypal_link(args)

    _assert_regenerate_route(regenerate.call_args)


def test_batch_regenerate_uses_method_owned_payment_route():
    args = _regenerate_args(email="", email_file="accounts.txt")

    with patch.object(cli, "CFG", _gopay_route_config()), \
         patch.object(cli, "_read_email_file", return_value=["batch@example.com"]), \
         patch(
             "sms_tool.paypal_links.regenerate_paypal_link",
             return_value={"ok": True},
         ) as regenerate:
        cli._regenerate_paypal_link(args)

    _assert_regenerate_route(regenerate.call_args)


def test_fallback_regenerate_uses_method_owned_payment_route():
    args = _regenerate_args(email="")

    with patch.object(cli, "CFG", _gopay_route_config()), patch(
        "sms_tool.paypal_links.regenerate_paypal_link",
        return_value={"ok": True},
    ) as regenerate:
        cli._regenerate_paypal_link_fallback(args, ["fallback@example.com"])

    _assert_regenerate_route(regenerate.call_args)
