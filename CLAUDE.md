# discord-mcp

Read-only MCP server that gives each person their Discord pings and
conversations from permitted channels they can see (an allowlist, or every
channel minus a deny list). Design: `docs/design.md`.

## Before every commit

1. Run the whole suite: `.venv/bin/pytest -q`. It must pass.
2. Run a code review of the change (`/code-review`, or a fresh reviewer agent
   on the most capable model), and fix Critical and Important findings.
3. Run a security review of the change (`/security-review`). Check in
   particular the guarantees in `docs/design.md`: read-only, no stored
   message text, minimal audit logs, fail closed, both access gates.
4. Check that nothing from the real Discord server is committed. This is a
   public repo: no real names, channel or guild IDs, message text, or
   `.env*`/`config.local.toml` content, including in docs and test fixtures.
5. In the commit message, say which reviews ran and what they found.

## Decisions

Record every major decision in `docs/decisions.md`, newest last, with the
date, the decision, why, and what was rejected. Add the entry in the same
commit as the change it describes.

## Conventions

- Tests first (red, then green). Tests use `tests/fakes.py`, never real data.
- Design docs go in `docs/superpowers/specs/`, plans in `docs/superpowers/plans/`.
- Error text shown to Claude is fixed text, never message content.
