from pathlib import Path
import json

# ==========================================
# Config
# ==========================================
def _load_config():
    candidates = [
        Path.cwd() / "config.json",
        Path(__file__).resolve().parent.parent / "config.json",
        Path(__file__).resolve().parent / "config.json",
    ]
    config_path = next((path for path in candidates if path.exists()), None)
    if config_path is None:
        searched = ", ".join(str(path) for path in candidates)
        print(f"[Error] config.json not found. Searched: {searched}")
        raise SystemExit(1)
    with open(config_path, "r", encoding="utf-8-sig") as f:
        return json.load(f)

CFG = _load_config()
