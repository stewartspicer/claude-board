"""Per-person bearer tokens. Only SHA-256 hashes are stored."""

import hashlib
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class User:
    id: int
    name: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return "cb_" + secrets.token_urlsafe(32)


def create_user(conn: sqlite3.Connection, name: str) -> str:
    """Create a user and return their token (shown once, never stored in clear)."""
    name = name.strip().lower()
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError("user names are letters, digits, '-' and '_' only")
    token = new_token()
    conn.execute(
        "INSERT INTO users (name, token_hash, created_at) VALUES (?, ?, ?)",
        (name, hash_token(token), datetime.now(UTC).isoformat(timespec="seconds")),
    )
    return token


def rotate_token(conn: sqlite3.Connection, name: str) -> str:
    token = new_token()
    cur = conn.execute("UPDATE users SET token_hash = ?, active = 1 WHERE name = ?", (hash_token(token), name.lower()))
    if cur.rowcount == 0:
        raise ValueError(f"no user named {name!r}")
    return token


def user_for_token(conn: sqlite3.Connection, token: str | None) -> User | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT id, name FROM users WHERE token_hash = ? AND active = 1", (hash_token(token),)
    ).fetchone()
    return User(row["id"], row["name"]) if row else None


def bearer(authorization: str | None) -> str | None:
    if authorization and authorization[:7].lower() == "bearer ":
        return authorization[7:].strip()
    return None


class BearerAuthMiddleware:
    """Reject requests without a valid token, except for public paths. Sets ``scope["board_user"]``.

    ``lock`` is the lock guarding the shared connection; lookups run in a worker thread under it.
    """

    def __init__(
        self,
        app: ASGIApp,
        conn: sqlite3.Connection,
        lock: threading.RLock,
        public_paths: frozenset[str] = frozenset(),
    ):
        self.app = app
        self.conn = conn
        self.lock = lock
        self.public_paths = public_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan" or (scope["type"] == "http" and scope["path"] in self.public_paths):
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # no websocket routes; refuse rather than pass through unchecked
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        token = bearer(headers.get("authorization"))
        user = await run_in_threadpool(self._lookup, token)
        if user is None:
            response = JSONResponse(
                {"error": "missing or invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        scope["board_user"] = user
        await self.app(scope, receive, send)

    def _lookup(self, token: str | None) -> User | None:
        with self.lock:
            return user_for_token(self.conn, token)
