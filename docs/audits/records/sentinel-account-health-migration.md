# Sentinel and account-health migration

## Sentinel runtime

Registration, recovery, phone registration, and checkout approval now issue
flow-bound tokens through `sms_tool.sentinel`. The module runs the pinned SDK
snapshot with the vendored Node VM runner. Device ID, cookies, fingerprint, and
flow are validated before a token is returned.

The default backend is:

```json
{
  "email_registration": {
    "sentinel_backend": "node_runner",
    "sentinel_legacy_fallback": true,
    "sentinel_prewarm_window": 0
  }
}
```

Set `sentinel_backend` to `legacy` only for temporary rollback. Legacy
QuickJS/browser/HTTP generation is no longer used by normal callers.

## Account-health jobs

After a successful account is saved, two deduplicated durable jobs are queued:

1. `plan`: checks plan and promotion state.
2. `deep_liveness`: runs the light AT probe, invokes the ordered recovery chain
   only for invalid tokens, then probes the recovered token again.

Queue state is stored in `runtime/account_health/queue.json`. Results are written
to the account's `account_health` object and never include credentials.

Disable automatic post-registration jobs with:

```json
{
  "account_health": {
    "post_registration_enabled": false
  }
}
```
