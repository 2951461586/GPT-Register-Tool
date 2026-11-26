## `env-loader-rs` in Rust

![env-loader-rs report card](card.svg)

| | |
| --- | --- |
| Unit | `sms_tool/kernel/env_loader.py` (Python) |
| Recorded cases matched | 205 / 205 |
| Speedup, call included | 1.7x (geometric mean) |
| Fallback to the original | 0.0% (0 of 205 cases) |
| Switched on | off |

| Function | Cases | Original per call | Rust per call | Speedup |
| --- | ---: | ---: | ---: | ---: |
| `_parse_lines` | 205 | 1.0 µs | 613 ns | 1.7x |

Measured with `rust-anything report` on x86_64, 12 cores, Windows: `diff` replays every recorded case, `bench` times them on both sides as callers call them. Reproduce: `rust-anything diff` and `rust-anything bench` in the crate folder.
