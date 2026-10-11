"""End-to-end over a real uvicorn server: auth, hook endpoints, and an MCP client round trip."""

import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from board.app import create_app


@pytest.fixture
def server(conn, tokens):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(create_app(conn), host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_healthz_and_setup_are_public_everything_else_needs_token(server, tokens):
    assert httpx.get(f"{server}/healthz").json() == {"ok": True}
    setup = httpx.get(f"{server}/setup").text
    assert "claude mcp add --scope user --transport http board http://localhost:8792/mcp" in setup
    assert "${CLAUDE_BOARD_TOKEN}" in setup and '"hooks": {' in setup
    assert httpx.post(f"{server}/hooks/prompt", json={}).status_code == 401
    assert httpx.post(f"{server}/mcp", json={}).status_code == 401
    assert httpx.post(f"{server}/hooks/prompt", json={}, headers=auth("cb_nope")).status_code == 401


def test_untrusted_host_header_is_rejected_by_mcp(server, tokens):
    r = httpx.post(
        f"{server}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={**auth(tokens["alice"]), "Host": "evil.example:80", "Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 421


def hook(server, token, path, session, cwd):
    r = httpx.post(f"{server}/hooks/{path}", json={"session_id": session, "cwd": cwd}, headers=auth(token))
    assert r.status_code == 200
    body = r.json()
    return body.get("hookSpecificOutput", {}).get("additionalContext", "")


def test_hooks_deliver_others_activity_once(server, tokens, board, alice, bob):
    board.create_task(alice, "media-hub", "Add series support", ref="Spec XYZ item 8")

    start = hook(server, tokens["bob"], "session-start", "c1", "/home/bob/code/media-hub")
    assert "board project 'media-hub'" in start
    assert "claimed by alice" in start and "ref: Spec XYZ item 8" in start
    assert "Activity by others" in start

    # Nothing new: the prompt hook adds nothing.
    assert hook(server, tokens["bob"], "prompt", "c1", "/home/bob/code/media-hub") == ""

    board.update_task(alice, 1, note="PR opened", link="#12")
    board.create_task(alice, "other-repo", "Unrelated")
    update = hook(server, tokens["bob"], "prompt", "c1", "/home/bob/code/media-hub")
    assert "PR opened" in update and "Unrelated" not in update
    assert hook(server, tokens["bob"], "prompt", "c1", "/home/bob/code/media-hub") == ""

    # Your own actions are never echoed back to you.
    assert hook(server, tokens["alice"], "prompt", "s1", r"C:\Users\alice\media-hub") == ""


def test_hooks_surface_notes_until_acked(server, tokens, board, alice, bob):
    note = board.post_message(alice, "Install the codeindex MCP (steps in README)", to="bob")
    start = hook(server, tokens["bob"], "session-start", "c1", "/anywhere")
    assert f"note #{note.id} from alice" in start and "user's approval" in start
    # Still shown at the next session start until acknowledged...
    assert "codeindex" in hook(server, tokens["bob"], "session-start", "c2", "/anywhere")
    board.ack_note(bob, note.id, "installed")
    assert "codeindex" not in hook(server, tokens["bob"], "session-start", "c3", "/anywhere")
    # ...and the sender hears back.
    assert "acknowledged note" in hook(server, tokens["alice"], "prompt", "s1", "/anywhere")


async def test_mcp_round_trip(server, tokens, board, bob):
    async def call(token, tool, **args):
        client = create_mcp_http_client(headers=auth(token))
        async with (
            client,
            streamable_http_client(f"{server}/mcp", http_client=client) as (read, write, *_),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(tool, args)
            return result.content[0].text

    created = await call(tokens["alice"], "create_task", project="shop", title="Refactor auth", ref="Spec A item 2")
    assert created.startswith("Created #1 [shop] Refactor auth — claimed by alice")

    conflict = await call(tokens["bob"], "claim_task", task_id=1)
    assert "claimed by alice" in conflict

    dup = await call(tokens["bob"], "create_task", project="shop", title="Auth rework", ref="spec a, item 2")
    assert "looks like an existing task" in dup

    found = await call(tokens["bob"], "find_tasks", query="spec a")
    assert "#1 [shop] Refactor auth" in found

    posted = await call(tokens["alice"], "post_message", text="install codeindex", to="bob")
    note_id = board.unacked_notes(bob)[0].id
    assert f"note #{note_id}" in posted
    status = await call(tokens["bob"], "board_status")
    assert "You are bob." in status and "awaiting ack_note" in status
    assert "Sent" in await call(tokens["bob"], "ack_note", note_id=note_id, reply="done")
    assert "status must be" in await call(tokens["alice"], "update_task", task_id=1, status="bogus")


def test_new_session_in_other_project_still_sees_its_events(server, tokens, board, alice):
    board.create_task(alice, "shop", "A")
    board.create_task(alice, "other", "B")
    hook(server, tokens["bob"], "session-start", "c1", "/code/shop")
    board.post_message(alice, "PR #12 merged, rebase before touching auth", project="other")
    assert hook(server, tokens["bob"], "prompt", "c1", "/code/shop") == ""
    start = hook(server, tokens["bob"], "session-start", "c2", "/code/other")
    assert "rebase before touching auth" in start


def test_bursts_beyond_the_limit_are_paged_not_dropped(server, tokens, board, alice):
    board.create_task(alice, "shop", "seed")
    hook(server, tokens["bob"], "session-start", "c1", "/code/shop")
    for i in range(14):
        board.post_message(alice, f"burst-{i:02d}", project="shop")
    first = hook(server, tokens["bob"], "prompt", "c1", "/code/shop")
    assert "burst-00" in first and "burst-09" in first and "burst-10" not in first and "more" in first
    second = hook(server, tokens["bob"], "prompt", "c1", "/code/shop")
    assert "burst-10" in second and "burst-13" in second
    assert hook(server, tokens["bob"], "prompt", "c1", "/code/shop") == ""


def test_allowed_hosts_follow_public_url(monkeypatch):
    from board.app import allowed_hosts, public_url

    monkeypatch.delenv("BOARD_PUBLIC_URL", raising=False)
    monkeypatch.delenv("BOARD_ALLOWED_HOSTS", raising=False)
    assert public_url() == "http://localhost:8792"
    assert allowed_hosts() == ["localhost:*", "127.0.0.1:*"]

    monkeypatch.setenv("BOARD_PUBLIC_URL", "http://myserver:8792/")
    monkeypatch.setenv("BOARD_ALLOWED_HOSTS", "192.168.1.10:*, myserver:*")
    assert public_url() == "http://myserver:8792"
    assert allowed_hosts() == ["localhost:*", "127.0.0.1:*", "myserver:*", "192.168.1.10:*"]


async def test_mcp_plan_workflow(server, tokens, board, bob):
    async def call(token, tool, **args):
        client = create_mcp_http_client(headers=auth(token))
        async with (
            client,
            streamable_http_client(f"{server}/mcp", http_client=client) as (read, write, *_),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return (await session.call_tool(tool, args)).content[0].text

    ready = await call(tokens["alice"], "define_plan", project="shop", plan="prd-checkout", branch="feature/checkout")
    assert ready.startswith("Plan ready: prd-checkout (branch feature/checkout): 0/0 done")
    for i, title in enumerate(["Cart model", "Checkout API"], start=1):
        await call(tokens["alice"], "create_task", project="shop", title=title, plan="prd-checkout",
                   ref=f"PRD item {i}", claim=False)  # fmt: skip
    await call(tokens["bob"], "claim_task", task_id=2)
    done = await call(tokens["bob"], "update_task", task_id=2, status="done", commit="abc1234")
    assert "commit abc1234" in done and "plan: prd-checkout" in done
    status = await call(tokens["alice"], "plan_status", project="shop", plan="prd-checkout")
    assert status.splitlines()[0] == "prd-checkout (branch feature/checkout): 1/2 done · 1 open"
    assert "#1 [shop] Cart model — open" in status.splitlines()[1]
    overview = await call(tokens["alice"], "board_status", project="shop")
    assert "Plans in shop:" in overview
