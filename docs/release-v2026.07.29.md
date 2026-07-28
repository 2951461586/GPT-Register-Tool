# v2026.07.29

## Protocol payment

- Added direct-card Checkout and MoMo VN/VND adapters to the unified payment manager, CLI, configuration UI, registration choices, storage detection, and desktop extraction window.
- MoMo runs through an isolated credential file, supports VN proxy routing, returns normalized decision states, and decodes scannable QR images into the runtime directory.
- BLIK now requires an explicit six-digit code, reports payment completion through a structured sentinel, and is presented as payment execution rather than link extraction.
- Protocol subprocesses now use configurable long-running timeouts and terminate their full process tree when timed out.

## Security and reliability

- Access tokens are passed from the desktop through temporary session files instead of process arguments.
- Backend command logs redact tokens, passwords, proxies, BLIK codes, and related credentials.
- Persisted payment history removes proxies and redacts BA tokens, bearer tokens, JWTs, authenticated proxy URLs, and embedded credential values.
- Payment-run persistence failures are reported as warnings without converting a successful upstream extraction into a failure.
- Fixed Settings save failure caused by a stale `default_proxy` field lookup after the field was removed from the UI.

## Validation

- Local account availability and payment eligibility are reported as separate stages. A successful non-401 token probe is not considered proof of MoMo availability.
- Live MoMo validation confirmed the VN proxy route and a complete `ready_with_qr` result with a `payment.momo.vn` URL and decoded PNG. Runtime account lists, URLs, reports, and QR files were removed before release.
- Full automated test suite, Python compilation, desktop publish, installer build, and diff checks are required before publishing this release.
