from __future__ import annotations

import importlib
import sys
from unittest.mock import patch

import pytest


def test_package_main_import_has_no_side_effect():
    sys.modules.pop("sms_tool.__main__", None)
    with patch("sms_tool.cli.main") as main:
        importlib.import_module("sms_tool.__main__")
    main.assert_not_called()


@pytest.mark.parametrize(
    "name",
    (
        "export_codex_session",
        "export_codex_sessions",
        "import_cpa_session",
        "import_cpa_sessions",
        "refresh_session",
        "auto_pay",
    ),
)
def test_cli_optional_boundaries_are_lazy_imported(name: str):
    import sms_tool.cli as cli

    assert not hasattr(cli, name)
