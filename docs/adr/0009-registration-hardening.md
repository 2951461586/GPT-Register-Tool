# ADR-0009: Explicit registration dependencies and scoped failure policy

- Status: Accepted
- Date: 2026-09-06

## Decision

- Replace whole-module workflow injection with immutable `RegistrationOperations`
  bound per invocation. Preserve facade imports and pre-invocation patch scopes.
- Group runtime state without changing persisted checkpoint/result schemas.
- Preserve `external_sessions` imports through a package separating lifecycle,
  profile configuration and egress audit.
- Keep child launch environment workarounds local to the child process.
- Share retry decisions, not mutable circuit state across unrelated lifetimes.
- Keep active phone/OAuth recovery and SMS registration paths.
- Retain the existing shard-first configuration contract and legacy migration.
- Add versioned correlation fields and explicit test/live provenance.

## Consequences

These changes retain ADR-0003's driver registry, ADR-0006's pool lifecycle and
ADR-0008's result builder. The operations Interface still exposes many helpers;
grouping those into higher-level operations and immutable stage results can
follow independently, without a breaking rewrite in this change.

Runtime cleanup is report-only. Live registration comparisons require samples
and budget confirmation and are not a CI activity.
