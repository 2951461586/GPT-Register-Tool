"""The desktop -> backend proxy transport, from the Python side.

A residential proxy URL carries ``user:pass``. The WPF host used to pass the
proxy flags as command-line arguments, and on Windows every same-user process
can read another process's command line (Task Manager / WMI), so the launcher
now exports the values and strips the flags. The registration pair was moved
first; the payment lane's ``--checkout-proxy-pool`` / ``--approve-proxy-pool``
carry whole pools of the same credentials and follow the same route.

These tests pin the receiving half: the names match the shared cross-language
fixture, a flag still wins over the environment, the variable is consumed so it
is not inherited by the child processes, and every transported flag is declared
sensitive in the shared sanction policy (otherwise the argument log prints it).
"""

from __future__ import annotations

import json
import os
from argparse import Namespace
from pathlib import Path

import pytest

from sms_tool import cli

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "proxy_env_transport.json"
POLICY = ROOT / "sensitive_policy.json"
SECRET_PROXY = "http://user:s3cret@us.ipwo.net:7878"
POOL = SECRET_PROXY + "\nhttp://other:pw@eu.ipwo.net:7878"


@pytest.fixture(autouse=True)
def _clean_transport_environment(monkeypatch):
    """No test may observe a transport variable left by another test."""
    for name, _attribute in cli._PROXY_TRANSPORT_ROUTES:
        monkeypatch.delenv(name, raising=False)


def _args(**overrides) -> Namespace:
    base = {"proxy": None, "proxy_pool": ""}
    base.update(overrides)
    return Namespace(**base)


def test_names_match_the_shared_cross_language_fixture():
    # ProxyArgumentTransport.cs reads the same file, so renaming one side without
    # the other fails a test instead of silently dropping the operator's route.
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert cli._PROXY_ENV == fixture["proxy_variable"]
    assert cli._PROXY_POOL_ENV == fixture["proxy_pool_variable"]
    assert cli._CHECKOUT_PROXY_POOL_ENV == fixture["checkout_pool_variable"]
    assert cli._APPROVE_PROXY_POOL_ENV == fixture["approve_pool_variable"]


def test_every_transported_flag_is_declared_sensitive_in_the_shared_policy():
    """A transported flag that is not in the policy is printed in full.

    ``SensitiveDataSanitizer.RedactArguments`` (task-log display) and the Python
    ``sanitize_values`` both read ``sensitive_policy.json``. The payment pools
    were missing from it, so the pool (credentials included) was rendered
    verbatim in the operator's log even before it reached argv.
    """
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    sensitive = {item.lower() for item in policy["sensitive_options"]}
    flags = [value for key, value in fixture.items() if key.endswith("_flag")]
    assert len(flags) == 4
    missing = [flag for flag in flags if flag.lower() not in sensitive]
    assert not missing, f"transported proxy routes missing from sensitive_options: {missing}"


@pytest.mark.parametrize("env_name,attribute", cli._PROXY_TRANSPORT_ROUTES)
def test_a_transported_route_is_taken_from_the_environment(monkeypatch, env_name, attribute):
    monkeypatch.setenv(env_name, POOL)
    args = _args(**{attribute: None if attribute == "proxy" else ""})

    cli._apply_proxy_argument_transport(args)

    assert getattr(args, attribute) == POOL


def test_scalar_proxy_is_taken_from_the_environment(monkeypatch):
    monkeypatch.setenv("SMS_TOOL_PROXY", SECRET_PROXY)
    args = _args()

    cli._apply_proxy_argument_transport(args)

    assert args.proxy == SECRET_PROXY


def test_pool_is_taken_from_the_environment_with_its_line_breaks(monkeypatch):
    monkeypatch.setenv("SMS_TOOL_PROXY_POOL", POOL)
    args = _args()

    cli._apply_proxy_argument_transport(args)

    assert args.proxy_pool == POOL


def test_an_explicit_flag_wins_over_the_transported_value(monkeypatch):
    monkeypatch.setenv("SMS_TOOL_PROXY", SECRET_PROXY)
    monkeypatch.setenv("SMS_TOOL_PROXY_POOL", POOL)
    args = _args(proxy="http://typed:in@host:1", proxy_pool="http://typed:2")

    cli._apply_proxy_argument_transport(args)

    assert args.proxy == "http://typed:in@host:1"
    assert args.proxy_pool == "http://typed:2"


def test_the_variable_is_consumed_so_children_cannot_inherit_it(monkeypatch):
    for env_name, _attribute in cli._PROXY_TRANSPORT_ROUTES:
        monkeypatch.setenv(env_name, POOL)

    cli._apply_proxy_argument_transport(_args())

    for env_name, _attribute in cli._PROXY_TRANSPORT_ROUTES:
        assert env_name not in os.environ


def test_blank_values_are_ignored(monkeypatch):
    monkeypatch.setenv("SMS_TOOL_PROXY", "   ")
    args = _args()

    cli._apply_proxy_argument_transport(args)

    assert args.proxy is None


def test_absence_of_the_variables_changes_nothing(monkeypatch):
    args = _args()

    cli._apply_proxy_argument_transport(args)

    assert args.proxy is None
    assert args.proxy_pool == ""


def test_subparsers_without_a_proxy_pool_attribute_are_left_alone(monkeypatch):
    # Only the registration subcommands define --proxy-pool; the rest must not
    # gain the attribute (a stray one would end up in **kwargs).
    monkeypatch.setenv("SMS_TOOL_PROXY", SECRET_PROXY)
    monkeypatch.setenv("SMS_TOOL_PROXY_POOL", POOL)
    args = Namespace(proxy=None)

    cli._apply_proxy_argument_transport(args)

    assert args.proxy == SECRET_PROXY
    assert not hasattr(args, "proxy_pool")


def test_the_transported_route_counts_as_explicit():
    # ``proxy_explicit`` is what stops ``_apply_registration_proxy_defaults`` from
    # replacing the operator's choice with the configured default, so a value that
    # arrives through the transport must set it exactly like a typed flag.
    args = _args(proxy=SECRET_PROXY)
    args.proxy_explicit = bool(args.proxy)

    assert args.proxy_explicit is True
