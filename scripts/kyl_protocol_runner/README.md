# KYL Protocol Runner

Reusable protocol-mode runner for the KYL/OpenAI sandbox authorization flow.

It saves Codex auth JSON files directly into `AUTH_DIR` and does not require manual browser import.

## Files

- `kyl_protocol_replay.py` — single-account protocol replay.
- `run_kyl_protocol_batch.js` — batch scheduler.
- `run_from_env.sh` — loads `.env` and runs the batch scheduler.
- `install_deps.sh` / `requirements.txt` — Python dependency installer.
- `example.env` — configuration template.

## Install

```bash
./install_deps.sh
```

## Configure

```bash
cp example.env .env
```

Edit `.env`:

```bash
STATE_PATH=/absolute/path/to/state.json
CDP_COOKIE_PATH=/absolute/path/to/cookies.json
KYL_FINGERPRINT=kyl-fp-REPLACE_ME
AUTH_DIR=/absolute/path/to/output-auths
PROTOCOL_WORKERS=2
```

`STATE_PATH` must contain:

```json
{
  "accounts": [
    { "email": "account@example.test", "sub": "subject-id" }
  ]
}
```

The fingerprint can also be stored as top-level `fingerprint` in the state JSON.

## Run

```bash
./run_from_env.sh
```

Retry more conservatively:

```bash
PROTOCOL_WORKERS=1 ./run_from_env.sh --workers 1
```

Resume from an index:

```bash
./run_from_env.sh --start 30
```

## Output

- Auth files: `AUTH_DIR`, default `./auths`.
- Status log: `runtime/protocol-batch.jsonl`.
- Existing auth emails are skipped by default.

Use `--include-existing=1` only when intentionally refreshing existing auth files.
