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

| Area | Capability |
| --- | --- |
| **Registration** | Mailbox pools / ReMail long-lived mailboxes / CFWorker domains / iCloud relay links plus multi-vendor SMS; single and concurrent batches; each account mints its own Sentinel token and `oai-did`; success means the AT probe answered HTTP 200; registration only stores the AT/Session and never builds payment links |
| **Protocol consistency and recovery** | Preflight of the ChatGPT / Auth / Sentinel hops before a mailbox is spent; one proxy session per account, rotated only on failure; NextAuth / Auth / ChatGPT share a stable `oai-did`, UA and client hints with GeoIP-derived locale and timezone; 403/429 opens a circuit with no plain-HTTP PoW fallback; the checkpoint is persisted before the AT probe so a resume never re-spends an OTP |
| **Mailbox and OTP** | One mailbox seam: ReMail, Smailr, CFWorker, Microsoft Graph/OAuth, Outlook/Hotmail IMAP, Gmail, iCloud relay links and legacy pool formats; OTP parsing filters by subject, sender, exact recipient and server timestamp, then ranks candidates; ReMail adds adaptive polling, detail-body fetch and a 30s resend |
| **Protocol payment links** | 15 methods: PayPal, GoPay, GCash, GrabPay, UPI, iDEAL, PIX, Kakao Pay, BLIK, TWINT, direct card Checkout, MoMo, QRIS, Bizum, Naver Pay (the last three are CLI canaries); Checkout and Approve use separate exit pools and rewrite country and Session per method; Checkout / Promotion / PM / Confirm / Poll / Provider Redirect stay distinct stages; five terminal states with `retryable` and `error_stage`, and `unknown` must be reconciled first |
| **Batch payment** | JIT AT gate, layered HTTP 401 recovery (RT -> Cookie -> isolated browser mailbox OTP -> Codex OAuth), eligibility matrix, Canary pause, per-method concurrency, atomic checkpoints and explicit resume; the report separates AT 200, eligibility, visible payment methods, approve and final link/QR artifacts |
| **Agent Identity and SUB2API** | The registration flow has no Agent Identity stage and its failure cannot change the AT 200 verdict; creation/rebuild only happens through the explicit SUB2API import; the Ed25519 PKCS#8 key is stored separately and never logged; import supports `auto` / `oauth` / `agent_identity` |
| **Accounts and data** | Session JSON plus SQLite dual index; liveness probe and 401 recovery; promotion and payment-eligibility badges (plan/trial only by default, an explicit entry point creates a one-shot Checkout to read payment-method evidence); Codex / CPA / SUB2API import-export; local data lives in `sessions/` and `runtime/`, both Git-ignored |
| **Desktop operations** | Selected accounts drive the "batch protocol payment" window (concurrency, retries, Canary, two exit pools, checkpoint resume); each `Saved session:` debounces an account-pool refresh; multi-select delete is one backend batch command |
| **SMS** | SMSBower, HeroSMS, Grizzly and NexSMS provider selection with online catalog lookup (availability depends on each vendor), plus send retry, timeout and poll-interval settings; see the [one-click SMS contract](docs/current/one-click-sms.md) |


### Architecture

Five layers — **① UI → ② CLI / command adapters → ③ application workflows → ④ domain contracts → ⑤ provider / persistence adapters**. Imports point inward only; a lower layer never imports a higher one. Configuration and runtime data live outside the repository (Git-ignored). Registration has two lanes — protocol (the built-in default) and browser drivers — sharing one mailbox-OTP, session-extraction and AT-probe boundary.

```mermaid
flowchart TB
    subgraph L1["① Desktop UI · SmsWorkbench/ (WPF · .NET 10)"]
        direction LR
        PANEL["Generic Host / DI · MVVM pages<br/>settings · batch-payment window · account pool"]
        IPC["IBackendClient<br/>ArgumentList + cancel / timeout / process-tree kill<br/>@@SMSWORKBENCH_V2@@ versioned envelope"]
    end

    subgraph L2["② CLI / command adapters"]
        direction LR
        CLI["cli.py · commands/ · cli_parsers/<br/>parsing · task orchestration · batches · exit status"]
    end

    subgraph L3["③ Application workflows"]
        direction LR
        REG["registration.py (facade)<br/>registration_handlers.py (protocol lane · stage order)"]
        DRV["registration_drivers/<br/>browser drivers: playwright · camoufox · roxy · cloak<br/>browser_flow/ · external_sessions/"]
        BATCH["batch_runner · registration_pulse<br/>concurrency · pulse · JIT AT gate · checkpoint resume"]
        REC["accounts/account_recovery · codex_oauth<br/>existing-account recovery"]
    end

    subgraph L4["④ Domain contracts (immutable seams)"]
        direction LR
        SEAM["registration_result · registration_flags<br/>checkout_contract · payment_wire · desktop_ipc<br/>RegistrationOperations (bound once per invocation)"]
    end

    subgraph L5["⑤ Provider / persistence adapters"]
        direction LR
        AF["auth_flow/<br/>steps: signin / authorize / continue / OTP / TOTP"]
        SEN["sentinel/<br/>Sentinel token issuance (Node sdk.js runner)"]
        MB["mailbox_service · mailbox_strategies<br/>providers/ (ReMail · Graph · IMAP · …)"]
        PAY["pay_link/ · paypal/ · paypal_link/ · upi_link/<br/>Checkout contract · capability probe · wallets / GCash"]
        STO["store/<br/>SQLite + Session JSON"]
        GEO["geo/ · proxy_routing · fingerprint_pool<br/>exit GeoIP · fingerprint binding"]
    end

    SVC["services/<br/>optional local protocol services (subprocess boundary)<br/>mail-otp-web · protocol-payment"]
    LOCAL[("Local config and runtime data<br/>proxy.json · runtime.json · payment.json<br/>sessions/ · runtime/ -- Git-ignored")]

    PANEL --> IPC --> CLI
    CLI --> REG
    CLI --> BATCH
    CLI --> REC
    REG --> AF
    REG --> SEN
    REG --> MB
    REG --> GEO
    REG --> STO
    REG -. driver registry .-> DRV
    DRV --> SEN
    BATCH --> PAY
    BATCH --> STO
    REG --> SEAM
    BATCH --> SEAM
    AF --> SEAM
    PAY --> SEAM
    STO --> LOCAL
    CLI -.-> SVC
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
