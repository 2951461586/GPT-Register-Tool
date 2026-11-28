## `codex-oauth-stage-rs` in Rust

![codex-oauth-stage-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/codex/oauth_helpers.py` (Python) |
| Recorded cases matched | 224 / 224 |
| Speedup, call included | 2.3x (geometric mean) |
| Fallback to the original | 0.0% (0 of 224 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_detect_protocol_stage` | 224 | 1.2 µs | 507 ns | 2.3x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.
