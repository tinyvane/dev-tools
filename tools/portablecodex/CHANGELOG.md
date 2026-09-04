# Changelog

## [0.1.0] - 2026-09-05

- Extracted Codex context diagnostics and portable workspace management from `codesync` into an
  independently installable package in the same `dev-tools` repository.
- Added guided `portablecodex onboard` with fail-closed `connect` and `initialize` paths.
- Preserved compatibility with existing schema-v1 `V:\CodexPortable` registrations and legacy
  codesync-managed `codexv.cmd` shims.
