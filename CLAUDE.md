# Claude Board: notes for Claude

A shared task/message board (MCP server plus Claude Code hooks) so collaborators' Claudes can claim work, post progress, and leave each other notes. README.md covers usage and design; the spec is in docs/specs/. Deployment-specific notes, if any, are in `CLAUDE.local.md` (gitignored).

## Workflow
- Tests: `uv run pytest`. Lint/format: `uv run ruff format . && uv run ruff check .`. Both must be clean; CI runs the same commands plus a Docker build.
- Work on a branch and open a PR.
- After deploying, verify with a real `claude -p` session against the server (see "End-to-end check"), not just the tests.

## Layout
- `board/board.py` holds the domain logic (pure, SQLite) and the formatting shared by tools and hooks. Every write runs in one transaction under `Board.lock` (the `@_atomic` decorator).
- Plans group a project's tasks (e.g. one PRD); `tasks.plan_id` and `tasks.commit_sha` were added after the first release, so new columns must also go in `db.MIGRATIONS`.
- `board/mcp_tools.py` wraps it in MCP tools. `board/hooks.py` builds the hook context and manages read cursors. `board/app.py` wires everything together and reads config. `board/admin.py` is the CLI.
- `board/setup.md` is the client setup guide, served at `/setup` with `{base}` filled in from `BOARD_PUBLIC_URL`. Literal braces are doubled (`str.format`).
- `deploy/` holds the Unraid deploy script and template. `docker-compose.yml` covers everything else. Deploy settings come from `.env` (see `.env.example`).

## Gotchas (verified 2026-10-10, Claude Code 2.1.266, mcp 2.3.0)
- Claude Code **HTTP hooks don't work** for this. They aren't supported on SessionStart, and they block private/tailnet addresses. Clients use `command` hooks running `curl --data-binary @-`.
- The client token lives in the `env` block of `~/.claude/settings.json`. That reaches both the hook commands and the `${CLAUDE_BOARD_TOKEN}` expansion in the MCP header.
- MCP SDK v2: `MCPServer` (FastMCP is gone). It's mounted at `/` with `streamable_http_path="/mcp"`, after the FastAPI routes; mounting at `/mcp` gives a redirect that breaks POST. Stateless plus JSON responses. The FastAPI lifespan must enter `mcp.session_manager.run()`. The `Host` allow-list comes from `allowed_hosts()` in app.py.
- Tools identify the caller from `ctx.headers["authorization"]`, resolved against stored token hashes. The ASGI middleware already rejects bad tokens with 401 on everything except `/healthz` and `/setup`.
- Hook cursors: keys are `session:<id>` and `project:<name>`. A cursor only advances past events that were actually shown (paging), so never jump it to the latest id while unshown events remain.
- `claude -p` with `--allowedTools a b` swallows the prompt if the prompt comes after it; put the prompt first.

## End-to-end check
Create `mcp.json` and `settings.json` from `board/setup.md` in a scratch folder named like a project. Then run `claude -p "<prompt>" --settings settings.json --strict-mcp-config --mcp-config mcp.json --allowedTools "mcp__board__*"` with a test user's token (`python -m board.admin add-user e2e-test` in the container; delete the user and its data afterwards).
