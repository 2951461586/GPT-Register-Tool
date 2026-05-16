from pathlib import Path
import json

# ==========================================
# Config
# ==========================================
def _load_config():
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        print(f"[Error] config.json not found at {config_path}")
        raise SystemExit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

CFG = _load_config()
