import importlib
import sys
import unittest
from unittest.mock import patch


class EntrypointTests(unittest.TestCase):
    def test_package_main_import_has_no_side_effect(self):
        sys.modules.pop("sms_tool.__main__", None)
        with patch("sms_tool.cli.main") as main:
            importlib.import_module("sms_tool.__main__")
        main.assert_not_called()

    def test_cli_optional_boundaries_are_lazy_imported(self):
        import sms_tool.cli as cli

        optional_modules = (
            "export_codex_session",
            "export_codex_sessions",
            "import_cpa_session",
            "import_cpa_sessions",
            "regenerate_paypal_link",
            "refresh_session",
            "auto_pay",
        )
        for name in optional_modules:
            with self.subTest(name=name):
                self.assertFalse(hasattr(cli, name))


if __name__ == "__main__":
    unittest.main()
