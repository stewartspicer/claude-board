"""ASGI app factory (`uvicorn board.app:create_app --factory`).

MCP over streamable HTTP at /mcp, Claude Code hooks at /hooks/*, client setup at /setup, health at /healthz.
"""

import contextlib
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool

from . import db
from .auth import BearerAuthMiddleware
from .board import Board
from .hooks import hook_response, prompt_context, session_start_context
from .mcp_tools import build_mcp


def public_url() -> str:
    """The URL clients use to reach the board (``BOARD_PUBLIC_URL``)."""
    return os.environ.get("BOARD_PUBLIC_URL", "http://localhost:8792").rstrip("/")


def allowed_hosts() -> list[str]:
    """Host headers /mcp accepts (DNS-rebinding protection): localhost, the public URL's host, and any
    extra ``host:port`` patterns in ``BOARD_ALLOWED_HOSTS`` (comma-separated, ``host:*`` for any port)."""
    hosts = ["localhost:*", "127.0.0.1:*"]
    public_host = urlsplit(public_url()).hostname
    if public_host:
        hosts.append(f"{public_host}:*")
    hosts += [h.strip() for h in os.environ.get("BOARD_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return list(dict.fromkeys(hosts))


def create_app(conn=None) -> FastAPI:
    conn = conn or db.connect()
    board = Board(conn)
    mcp = build_mcp(board)
    hosts = allowed_hosts()
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[]),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="Claude Board", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    setup_text = (Path(__file__).parent / "setup.md").read_text(encoding="utf-8").format(base=public_url())

    @app.get("/setup", response_class=PlainTextResponse)
    def setup():
        return setup_text

    @app.post("/hooks/session-start")
    async def session_start(request: Request):
        payload = await _json(request)
        user = request.scope["board_user"]
        text = await run_in_threadpool(_locked, board, session_start_context, user, payload)
        return hook_response("SessionStart", text)

    @app.post("/hooks/prompt")
    async def prompt(request: Request):
        payload = await _json(request)
        user = request.scope["board_user"]
        text = await run_in_threadpool(_locked, board, prompt_context, user, payload)
        return hook_response("UserPromptSubmit", text)

    app.mount("/", mcp_app)  # after the routes above, so it only serves /mcp
    app.add_middleware(BearerAuthMiddleware, conn=conn, lock=board.lock, public_paths=frozenset({"/healthz", "/setup"}))
    app.state.board = board
    return app


def _locked(board: Board, build, user, payload: dict) -> str:
    """Build hook context in a worker thread, holding the board lock so the read and cursor update are atomic."""
    with board.lock:
        return build(board, user, payload.get("session_id"), payload.get("cwd"))


async def _json(request: Request) -> dict:
    try:
        payload = await request.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
