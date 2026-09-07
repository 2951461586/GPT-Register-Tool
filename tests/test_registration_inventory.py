import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "registration_inventory", Path(__file__).resolve().parents[1] / "scripts" / "registration_inventory.py",
)
inventory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inventory)


def test_inventory_only_reports_counts_and_preserves_files(tmp_path):
    pool = tmp_path / "mailbox_tokens.txt"
    pool.write_text(
        "gmail://a@gmail.com----test-app-password\n"
        "gmail://a@gmail.com----test-app-password\n"
        "remail://b@example.com---test-token---test-order\n",
        encoding="utf-8",
    )
    before = pool.read_bytes()
    counts = inventory.candidate_counts(pool)
    assert counts["providers"] == {"gmail": 1, "remail": 1}
    assert "@" not in str(counts)
    cleanup = inventory.cleanup_inventory(tmp_path)
    assert cleanup["files_modified"] == 0
    assert cleanup["categories"]["preserve_state_credentials_and_evidence"]["files"] == 1
    assert pool.read_bytes() == before
