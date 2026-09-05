from scripts.docs_consistency_scan import main


def test_live_documentation_pointers_are_current():
    assert main() == 0


def test_provider_compatibility_facades_alias_the_implementation_modules():
    import importlib

    for name in ("cfworker", "gmail", "graph", "icloud_url", "remail", "smailr"):
        facade = importlib.import_module(f"sms_tool.mailbox_{name}")
        implementation = importlib.import_module(f"sms_tool.providers.mailbox_{name}")
        assert facade is implementation
    assert importlib.import_module("sms_tool.outlook_imap") is importlib.import_module("sms_tool.providers.outlook_imap")
