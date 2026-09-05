# Local Runtime Files

This directory is reserved for machine-local configuration and runtime state.
Keep credentials, mailbox tokens, sessions, SQLite databases, logs, and browser
profiles out of source control and out of release payloads. Use the repository
templates (`config.example.json`, `.env.example`) to create local values.

The current compatibility paths at the repository root remain supported by the
runtime; new tooling should prefer `runtime/` and `sessions/` for generated data.
