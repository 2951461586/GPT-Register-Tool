# Third-party notices

`runtime/sentinel-runner.js` and the pinned `runtime/sdk.js` snapshot were
imported from `myfanhua/turb-gpt-free-register` at the repository state fetched
on 2026-08-25. The reference repository is distributed under the MIT License.

The SDK snapshot originates from OpenAI's public Sentinel web asset. Its
checksum is pinned in `bundle.py` so an upstream rotation cannot silently
change protocol behaviour. Before redistributing binary/source bundles outside
this project, review the upstream asset's applicable terms.

## Local patches to `runtime/sentinel-runner.js`

1. **2026-11-11 — `performance.memory` is Chromium-only.** The vendored runner
   exposed it on every profile, so a Firefox or Safari UA reported
   `performance.memory.jsHeapSizeLimit` — an API Gecko and WebKit do not
   implement. The object is now built only for Chromium profiles, which is what
   a real browser does. `bundle.py:RUNNER_SHA256` pins the patched bytes; a
   re-vendor must re-apply this patch and update that constant, otherwise
   `validate_runtime_bundle()` fails loudly with
   `sentinel_runtime_hash_mismatch`.
