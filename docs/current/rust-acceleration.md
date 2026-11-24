# Native Rust Acceleration (`native/sentinel_vm/` & `rust/`)

This repository uses Rust in two complementary ways to eliminate external runtime dependencies and accelerate hot CPU-bound Python paths while preserving 100% behavioral parity and pure-Python fallbacks:

1. **In-process Sentinel VM (`native/sentinel_vm/`)** — a compiled pure-Rust `cdylib` (`sms_tool/sentinel/runtime/sentinel_vm.dll`, loaded via `sms_tool/sentinel/native_ffi.py` and `sms_tool/sentinel/runner.py`) that executes OpenAI Sentinel PoW, Turnstile `dx`, and Session Observer `so` challenges in-process with **zero Node.js runtime dependency**. Binary integrity is pinned by SHA-256 in `sms_tool/sentinel/bundle.py` (`NATIVE_LIB_SHA256`) and checked by `scripts/sentinel_asset_guard.py`.
2. **Hot-path PyO3 extensions (`rust/*-rs/`)** — fourteen focused Python extension modules managed by `rust-anything` (PyO3 `abi3-py39`, `0 unsafe`, static CRT with zero MinGW runtime DLL dependencies). Every unit ships only functions that empirically clear **100% diff parity** and **≥ 1.1× end-to-end speedup** (FFI crossing included).

## Active `rust-anything` PyO3 Units (14 Crates · 26 Functions)

| Crate | Python Owner | Accelerated Functions | Measured Speedup (Geo Mean) | Recorded Cases | Gate |
| --- | --- | --- | ---: | ---: | --- |
| `rust/sanitize-text-rs/` | `sms_tool/kernel/sanitizer.py` | `sanitize_text` (8.9×), `sanitize_log_text` (13×), `sanitize` (5.0×) | **8.3×** | 1,038 / 1,038 | 12/12 PASS |
| `rust/mail-otp-rs/` | `sms_tool/mailbox/mail_otp.py` | `_extract_otp_from_text` (63×), `_looks_fake_otp_context` (2.2×), `_message_looks_fake_otp_noise` (2.3×) | **6.9×** | 353 / 353 | 12/12 PASS |
| `rust/error-classification-rs/` | `sms_tool/kernel/error_classification.py` | `classify_error` (5.3×), `is_terminal_registration_error` (7.4×) | **6.2×** | 851 / 851 | 12/12 PASS |
| `rust/normalize-proxy-url-rs/` | `sms_tool/proxy/entry.py` | `normalize_proxy_url` (2.9×), `redact_proxy_url` (4.3×), `strip_session_id` (5.0×), `infer_proxy_country` (5.2×) | **4.3×** | 1,843 / 1,843 | 12/12 PASS |
| `rust/cfworker-html-rs/` | `sms_tool/providers/cfworker_client.py` | `_display_text` (8.9×), `_strip_leading_css` (4.3×), `_extract_emails` (1.7×) | **4.0×** | 122 / 122 | 12/12 PASS |
| `rust/icloud-html-rs/` | `sms_tool/providers/mailbox_icloud_url.py` | `_strip_html_to_visible_text` (3.5×) | **3.5×** | 25 / 25 | 12/12 PASS |
| `rust/gmail-html-rs/` | `sms_tool/providers/mailbox_gmail.py` | `_html_to_text` (3.0×) | **3.0×** | 20 / 20 | 12/12 PASS |
| `rust/proxy-screening-rs/` | `sms_tool/proxy/screening.py` | `classify_network_type` (2.6×) | **2.6×** | 45 / 45 | 12/12 PASS |
| `rust/sms-code-rs/` | `sms_tool/phone/sms_utils.py` | `_extract_sms_code` (2.4×) | **2.4×** | 58 / 58 | 12/12 PASS |
| `rust/data-saver-rs/` | `sms_tool/registration_drivers/data_saver.py` | `is_cacheable_static_asset` (2.3×) | **2.3×** | 43 / 43 | 12/12 PASS |
| `rust/session-converter-rs/` | `sms_tool/session/converter.py` | `parse_jwt_payload` (1.4×), `collect_session_like_objects` (2.5×) | **1.9×** | 135 / 135 | 12/12 PASS |
| `rust/store-normalize-rs/` | `sms_tool/store/normalize.py` | `_normalize_account_email` (1.6×), `_looks_codex_refresh_token` (2.8×), `_jwt_account_type` (1.7×) | **1.9×** | 192 / 192 | 12/12 PASS |
| `rust/http-utils-rs/` | `sms_tool/transport/http_utils.py` | `_is_allowed_callback_url` (2.2×), `_minimal_chatgpt_cookie_header` (1.5×) | **1.8×** | 76 / 76 | 12/12 PASS |
| `rust/cpa-classify-rs/` | `sms_tool/importers/cpa.py` | `classify_cpa_auth_file` (1.7×) | **1.7×** | 23 / 23 | 12/12 PASS |

Total across all 14 crates: **4,824 / 4,824 recorded cases matched (0.0% fallback)**, **168 / 168 `rust-anything gate` checks passing**, **0 `unsafe` blocks**, and **0 external MinGW runtime DLL dependencies**.

Candidates benchmarked and deliberately left in Python (100% diff parity, but < 1.1× end-to-end speedup or tie due to PyO3 call-crossing cost on tiny dicts/strings/lists): `mailbox/parsers.py` (0.72×), `payment/promotion_states.py` (0.55×), `transport/auth_state.py` (0.58×), `session/converter.py::strip_unavailable` (0.31×), `payment/checkout_contract.py::_collect_method_group` (0.31×), `mailbox_icloud_url.py::{_api_payload_text,_walk_api_code}` (0.91× / 0.96×), and `mailbox_gmail.py::_normalize_recipients` (1.10× tie).

## Runtime Switches & Graceful Fallback

Every accelerated Python module loads its native extension at import time and falls back cleanly to the pure-Python implementation if the compiled `_native.pyd` is absent (for example in a pure-Python CI checkout without a Rust build):

- `RUST_ANYTHING_MODE=old` (or `original`): force all 14 units onto their pure-Python implementations (used for differential testing and re-recording).
- `RUST_ANYTHING_MODE=new` (or `rust`): require native extensions to load and raise immediately on import error.
- Per-unit override: `RUST_ANYTHING_UNIT_<UNIT>=old|new` (for example `RUST_ANYTHING_UNIT_CFWORKER_HTML=old`, `RUST_ANYTHING_UNIT_DATA_SAVER=old`).

## Verification Commands

```powershell
# Native Sentinel VM unit tests
cargo test --manifest-path native/sentinel_vm/Cargo.toml --release

# Verify any rust-anything crate (run sequentially, not concurrently with pytest on Windows)
Push-Location rust/cfworker-html-rs
rust-anything diff
rust-anything bench
rust-anything gate
Pop-Location

# Dual-mode Python vs Rust parity tests
python -m pytest -q tests/governance/test_proxy_screening_rust_parity.py tests/governance/test_html_extractors_rust_parity.py tests/governance/test_wave2_rust_parity.py
```

Detailed per-crate report cards live in each crate's `report/REPORT.md` and `report/card.svg`.
