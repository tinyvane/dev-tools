# Codex Repository Instructions

## Codex Role

- Work as an independent reviewer and hardening engineer. Prioritize bugs, unsafe assumptions, failure recovery, tests, security, and performance.
- Do not assume Claude Code's architecture or implementation is correct. Verify behavior from code, state transitions, and reproducible tests.
- Do not create agent-to-agent coordination workflows. Git commits and the repository are the handoff boundary; the user coordinates responsibilities.
- Keep fixes scoped and evidence-driven. Large feature construction is normally handled by Claude Code unless the user explicitly assigns it to Codex.

## Delivery

- Source changes are complete only after focused tests, the full test suite, a commit, and a push to `origin/main`.
- Keep the local installed `codesync` on the same pushed commit; verify with `codesync --version` and a read-only smoke test.
- Never copy development `.env` values into production configuration. This repository currently has no deployment `.env` files.

## Verification

- Run workspace tests with `$env:PYTHONPATH='src'; pytest -q` on PowerShell. A plain `pytest` may import an older globally installed package.
- Run `git diff --check` before committing.
- Update `CHANGELOG.md`, `README.md`, and the relevant engineering notes in `CLAUDE.md` when behavior changes.
- Bump `pyproject.toml` for every published behavior change.

## Repository Trash Safety

- GitHub names are not identities; use the immutable Repository ID for cross-machine decisions.
- Absence from a GitHub list is never authorization to remove or move a local directory. Require an explicit archived trash record.
- Repository removal means rename+archive remotely and whole-directory move into `.codesync-trash` locally. Permanent deletion exists only in explicit `trash purge`.
- Path resolution, GitHub identity, state schema, scan health, and version checks must fail closed.
- Preserve pending intent across `--no-push`, transient GitHub failures, and state updates.
- Read the current v2.17+ invariants in `CLAUDE.md` before changing `delete.py`, `trash.py`, `state.py`, or `github_auto.py`.
