<div align="center">
  <img src="./SmsWorkbench/Assets/black-kitten.png" width="140" alt="GPT-Register-Tool logo" />
  <h1>GPT-Register-Tool</h1>
  <p><strong>A Windows desktop workbench for ChatGPT account registration, email OTP, account management, and payment workflows</strong></p>
  <p>
    <a href="./README.md">简体中文</a> · <a href="./README_EN.md">English</a>
  </p>
  <p>
    <img src="https://img.shields.io/badge/Windows-10%2F11-0078D4?logo=windows&logoColor=white" alt="Windows 10/11" />
    <img src="https://img.shields.io/badge/.NET-10-512BD4?logo=dotnet&logoColor=white" alt=".NET 10" />
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+" />
  </p>
</div>

## Introduction

GPT-Register-Tool combines a **WPF desktop client with a Python core** for email OTP registration, account and Session management, proxy configuration, payment-link extraction, and account export. Runtime data is stored locally by default and is not committed to Git.

## Sponsor

<img width="5728" height="672" alt="IPWO residential proxy" src="https://github.com/user-attachments/assets/5f3b5b22-5132-4bc4-b8b8-3a0e92b47f37" />

[IPWO](https://www.ipwo.net) provides global residential proxy resources for ChatGPT automation tools, with multi-region IP selection and flexible proxy configuration.<br>
It is suitable for registration proxies, isolated network environments, and automation tasks that require project-specific network exits.<br>
Dynamic and static IP resources are available with free testing through the [IPWO trial portal](https://www.ipwo.net/?ref=githubGPT).

## Highlights

- Register accounts from mailbox pools, ReMail, or CFWorker sources.
- Poll OTP messages from Microsoft, Gmail, iCloud relay links, ReMail, and CFWorker.
- Manage local accounts, Sessions, quota status, and payment links from a Windows desktop client.
- Route registration, mailbox, Checkout, and Approve traffic through independently configured proxies.
- Extract supported payment links and export account data for Codex, CPA, and SUB2API workflows.
- Start fresh payment batches by default, or explicitly resume a matching persisted checkpoint with account-level stage progress.
- Probe PayPal capability and zero-due eligibility before the full flow; rebuild Checkout after an explicit blocked approval instead of re-approving the same submission.

## Requirements

- Windows 10/11 x64.
- Python 3.10 or later.
- .NET 10 Desktop Runtime; the .NET 10 SDK is required when building from source.
- Node.js 18 or later available on `PATH`.
- Playwright Chromium for browser-assisted payment workflows and browser registration.

## Installation

Download the latest installer or portable archive from [GitHub Releases](https://github.com/2951461586/GPT-Register-Tool/releases), or build the desktop application from source:

```powershell
git clone https://github.com/2951461586/GPT-Register-Tool.git
cd GPT-Register-Tool
python -m pip install -r requirements.txt -c constraints.txt
copy config.example.json config.json
powershell -ExecutionPolicy Bypass -File .\SmsWorkbench\build_dotnet.ps1
.\dist\net10\SmsWorkbench.exe
```

The supported desktop build command is `SmsWorkbench/build_dotnet.ps1`. Its output is written to `dist/net10/SmsWorkbench.exe`.

## Configuration ownership

The active configuration lives in `proxy.json`, `runtime.json` and
`payment.json`. If any shard exists, only existing shards are merged, in that
order. Legacy `config.json` is used and migrated only when no shards exist.
Do not edit a legacy file expecting it to override active shards.

`config_schema.json` is a shared ownership manifest, not JSON Schema. Runtime
validation remains in Python config/driver preflight. Local configuration files
contain credentials and must stay ignored. See
[configuration ownership](docs/current/configuration.md).

## Tests and release checks

From a configured source checkout:

```powershell
python -m sms_tool --help
python -m pytest -q
dotnet test GPTRegisterTool.slnx -c Release
python scripts/precommit_guard.py --all
python scripts/architecture_scan.py
python scripts/config_schema_check.py
python scripts/ipc_schema_check.py
python scripts/docs_consistency_scan.py
```

Default tests are offline. Live signup, mailbox spending and payment actions
require separate authorization. Use `scripts/build_installer.ps1 -Version <tag>`
only from the intended clean release revision; never include local runtime
data in release assets.

## Logs and local inventory

Backend JSON file logs and registration progress include `schema_version`,
`source`, `command_id` and `run_id`. Desktop process logs include `command_id`.
Explicit test rows are excluded from quality metrics; historical mixed rows do
not establish a reliable live success rate.

`python scripts/registration_inventory.py` prints provider candidate counts and
runtime file-category totals only. It does not test credentials, create
accounts, move or delete files. See
[telemetry and runtime data](docs/current/telemetry-and-runtime.md).

## Documentation

The Chinese README contains the complete feature, configuration, architecture, CLI, testing, and release documentation:

- [Complete Chinese documentation](./README.md)
- [Architecture](./docs/architecture.md)
- [v2026.09.10 release notes](./docs/releases/release-v2026.09.10.md)
- [Documentation index](./docs/README.md) (Chinese; release notes and audits archived under `docs/releases/` and `docs/audits/`)
- [Directory map](./docs/directory-map.md)
- [Proxy guide](./PROXY_GUIDE.md)

## Data And Responsible Use

Local configuration, mailbox credentials, proxy passwords, API keys, Tokens, Sessions, and runtime data must not be committed or shared publicly. Use this project only with authorization and in compliance with applicable service terms, regional laws, and organizational policies.
### Registration drivers

The desktop **Settings -> Registration & mailbox -> Registration driver** selector keeps `protocol` as the default and also exposes independent browser drivers:

- `playwright`: launch local Chromium through Playwright.
- `roxy`: create/open a RoxyBrowser profile through its local API and attach over CDP.
- `cloak`: use the installed CloakBrowser Python SDK.
- `camoufox`: use the installed Camoufox anti-detect browser (default browser driver).

Each driver reuses the mailbox OTP, session extraction, AT HTTP 200 probe, and persistence boundary. Provider credentials and lifecycle flags are configured in their own Settings sections. Missing required fields produce sanitized configuration errors; browser drivers do not bypass CAPTCHA and return `manual_challenge_required` when a human challenge is encountered.

Browser session implementation now lives in `external_sessions/` behind the
same factory import. Camoufox cleans up only profiles it created temporarily;
configured persistent profiles are never deleted. See
[registration architecture](docs/current/registration-architecture.md).
