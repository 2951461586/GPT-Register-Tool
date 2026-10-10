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
Current release notes: [v2026.10.08](./docs/releases/release-v2026.10.08.md).


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
- Promotion checks are plan-only by default. Explicitly select accounts and use `--check-payment-eligibility --email <address>` (or `--email-file <path>`) to observe Checkout methods without confirming a payment; this can trigger rate limits.

### Architecture (condensed)

```text
SmsWorkbench/            WPF desktop: Generic Host/DI -> MVVM pages, settings, task entry, state
        |  IBackendClient (ArgumentList + cancel/timeout/process-tree kill, @@SMSWORKBENCH_V2@@ envelope)
        v
sms_tool/cli.py          CLI and task orchestration: argument parsing, batches, exit status
        |
        +- registration_handlers.py  protocol stage order (auth_flow -> user/register -> OTP -> create_account -> session -> AT probe)
        |    +- auth_flow/           step functions: signin / authorize / continue / OTP / TOTP
        |    +- sentinel/            Sentinel token issuance (Node sdk.js runner)
        |    +- accounts/            account creation, liveness probe, recovery
        +- mailbox.py                mailbox routing: ReMail / CFWorker / Graph / Gmail / iCloud relay
        +- payment_*                 protocol payment: Checkout contract, capability probe, wallets/GCash, batch and JIT AT
        +- storage.py                SQLite + Session JSON persistence
services/                optional local protocol services: mail diagnosis, payment extractors (subprocess boundary)
```

See [architecture](docs/architecture.md) for boundaries and [directory map](docs/directory-map.md) for ownership.

## Installation

### Requirements

- Windows 10/11 x64.
- Python 3.10 or later.
- .NET 10 Desktop Runtime; the .NET 10 SDK is required when building from source.
- Node.js 18 or later available on `PATH`.
- Playwright Chromium for browser-assisted payment workflows and browser registration.

### Installation

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

### Configuration ownership

The active configuration lives in `proxy.json`, `runtime.json` and
`payment.json`. If any shard exists, only existing shards are merged, in that
order. Legacy `config.json` is used and migrated only when no shards exist.
Do not edit a legacy file expecting it to override active shards.

`config_schema.json` is a shared ownership manifest, not JSON Schema. Runtime
validation remains in Python config/driver preflight. Local configuration files
contain credentials and must stay ignored. See
[configuration ownership](docs/current/configuration.md).


## License And Responsible Use

Released under the **MIT License**; the full text is in [LICENSE](LICENSE).

```text
MIT License

Copyright (c) 2026 GPT-Register-Tool contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Use this project only with authorization and in compliance with applicable service terms, regional laws, and organizational policies. You are responsible for third-party mailbox, proxy, SMS and payment costs, account security, and data compliance; the sample configuration contains no real credentials.
