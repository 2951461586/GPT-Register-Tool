import json
from pathlib import Path


def test_mailbox_pool_liveness_requires_two_definitive_observations_before_apply(tmp_path, monkeypatch):
    import scripts.mailbox_pool_liveness as probe

    pool = tmp_path / "mailbox_tokens.txt"
    pool.write_text("user@icloud.com----https://mail.example/token\n", encoding="utf-8")
    report = tmp_path / "first.json"
    quarantine = tmp_path / "quarantine.json"

    monkeypatch.setattr(
        probe,
        "_fetch_mailbox_messages",
        lambda mailbox, limit=1, proxy=None: (_ for _ in ()).throw(RuntimeError("HTTP 404")),
    )
    monkeypatch.setattr(probe, "_quarantine_path", lambda value: quarantine)
    monkeypatch.setattr(probe.sys, "argv", ["mailbox_pool_liveness.py", "--pool-file", str(pool), "--apply", "--report", str(report)])
    assert probe.main() == 0
    assert pool.read_text(encoding="utf-8").strip()
    saved = json.loads(quarantine.read_text(encoding="utf-8"))
    assert saved["entries"]
    assert next(iter(saved["entries"].values()))["confirmations"] == 1

    monkeypatch.setattr(probe.sys, "argv", ["mailbox_pool_liveness.py", "--pool-file", str(pool), "--apply", "--report", str(report)])
    assert probe.main() == 0
    assert pool.read_text(encoding="utf-8") == ""
