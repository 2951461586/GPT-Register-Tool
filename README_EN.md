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

