# Changelog

## [0.1.1] - 2026-09-07

- Keep encrypted `secrets/*.age` files in per-PC/per-user LocalApplicationData while sharing
  V: sessions, memory and SQLite. Preserve legacy encrypted files and restore them only on
  the registered original machine/profile without overwriting subsequent logins.
- Guard credential redirection with shared lifetime leases, legacy-process detection,
  managed junction validation, atomic registration and retryable archival recovery.
- Exclude `secrets` from future home migrations; preserve CLI argument passthrough without
  PowerShell partial parameter binding (`-c` is no longer mistaken for `CodexArgs`).

## [0.1.0] - 2026-09-05

- Extracted Codex context diagnostics and portable workspace management from `codesync` into an
  independently installable package in the same `dev-tools` repository.
- Added guided `portablecodex onboard` with fail-closed `connect` and `initialize` paths.
- Preserved compatibility with existing schema-v1 `V:\CodexPortable` registrations and legacy
  codesync-managed `codexv.cmd` shims.
