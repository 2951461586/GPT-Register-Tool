# Mailbox Architecture

## Ownership

`MailboxService` is the application Interface used by registration and browser
workflows. `mailbox_strategies` owns the immutable provider registry.
`providers/mailbox_*` Modules own provider wire behaviour. Compatibility
facades remain at the top level under ADR-0001.

Each provider adapter declares three independent capabilities:

- message fetch
- OTP polling
- inbox credential readiness

Resolution uses two passes. A named provider wins first; the Microsoft Graph
adapter is a fallback and never needs an exclusion list of other providers.

## Configuration

`MailboxService.create` resolves one immutable `RuntimeConfig` and installs it
for the workflow scope. Provider code reads the current scoped configuration,
not a module snapshot. Tests and callers may inject a separate registry and
runtime configuration.

## Polling errors

`mailbox_errors.mailbox_error_disposition` is the single owner of polling
failure disposition:

- credential or expired-token failures terminate immediately and quarantine
  the mailbox;
- endpoint-unavailable failures terminate the poll and open endpoint cooldown;
- transport and empty-inbox observations remain retryable until the polling
  deadline.

Provider-specific polling remains intentionally different where the remote
protocol differs. ReMail keeps adaptive server-directed polling; Graph,
iCloud and other compatible providers use the shared settle loop.

## Proxy candidates

The configured mailbox proxy remains first. When
`email_registration.mailbox_proxy_fallback_to_operation_proxy` is enabled
(default), the operation proxy is appended as a fallback. This is not a claim
that mailbox and registration traffic always share one exit.

## Machine-readable contract

The table is checked against executable behaviour. Update code and this table
in the same change when one of these semantics changes.

<!-- mailbox-contract:start -->
| Key | Value |
| --- | --- |
| `provider_resolution` | `specific_then_fallback` |
| `auth_invalid` | `terminal_quarantine` |
| `endpoint_unavailable` | `terminal_cooldown` |
| `other_errors` | `retry_until_deadline` |
| `operation_proxy_fallback_default` | `true` |
| `proxy_order` | `mailbox_proxy,mailbox_proxy_pool,operation_proxy` |
<!-- mailbox-contract:end -->
