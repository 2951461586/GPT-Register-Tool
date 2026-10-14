from scripts.config_schema_check import main


def test_config_schema_matches_python_and_csharp_owners():
    assert main() == 0
