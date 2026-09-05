from scripts.ipc_schema_check import main


def test_ipc_schema_manifest_matches_protocol_sources():
    assert main() == 0
