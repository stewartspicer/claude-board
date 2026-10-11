"""Plans (a PRD's tasks as a group, with progress) and recording the commit when work lands."""

import sqlite3

import pytest

from board import db
from board.board import BoardError, format_event, format_plan_summary, format_task
from board.hooks import prompt_context, session_start_context

PRD_ITEMS = ["Cart model", "Checkout API", "Payment form", "Order emails", "Admin refunds"]


def load_prd(board, user, claim=False):
    board.define_plan(user, "shop", "prd-checkout", doc="docs/prd-checkout.md", branch="feature/checkout")
    return [
        board.create_task(user, "shop", title, ref=f"PRD Checkout item {i}", plan="prd-checkout", claim=claim)
        for i, title in enumerate(PRD_ITEMS, start=1)
    ]


def test_define_plan_creates_and_updates(board, alice):
    plan = board.define_plan(alice, "shop", "PRD-Checkout", doc="docs/prd.md")
    assert (plan.name, plan.project, plan.doc, plan.branch) == ("prd-checkout", "shop", "docs/prd.md", None)
    plan = board.define_plan(alice, "shop", "prd-checkout", branch="feature/checkout")
    assert (plan.doc, plan.branch) == ("docs/prd.md", "feature/checkout")  # unspecified fields are kept
    assert [p.name for p in board.plans("shop")] == ["prd-checkout"]


def test_plan_names_are_slugs(board, alice):
    with pytest.raises(BoardError, match="Plan names"):
        board.define_plan(alice, "shop", "my plan!")


def test_tasks_need_a_defined_plan(board, alice):
    board.create_task(alice, "shop", "seed")
    with pytest.raises(BoardError, match="No plan named 'prd-x' in shop"):
        board.create_task(alice, "shop", "Item", plan="prd-x")


def test_plan_tasks_in_prd_order_with_summary(board, alice, bob):
    tasks = load_prd(board, alice)
    board.claim_task(alice, tasks[0].id)
    board.claim_task(bob, tasks[2].id)
    board.claim_task(alice, tasks[3].id)
    board.update_task(alice, tasks[0].id, status="done", commit="abc1234")

    listed = board.tasks("shop", plan="prd-checkout", statuses=("open", "claimed", "done", "dropped"))
    assert [t.title for t in listed] == PRD_ITEMS  # PRD order, not activity order

    summary = board.plan_summary("shop", "prd-checkout")
    assert (summary.done, summary.total, summary.open) == (1, 5, 2)
    assert summary.claimed == {"alice": [tasks[3].id], "bob": [tasks[2].id]}
    text = format_plan_summary(summary)
    assert text.startswith("prd-checkout (branch feature/checkout, doc docs/prd-checkout.md): 1/5 done")
    assert f"alice #{tasks[3].id}" in text and f"bob #{tasks[2].id}" in text and "2 open" in text


def test_commit_recorded_and_announced(board, clock, alice, bob):
    tasks = load_prd(board, alice)
    board.claim_task(bob, tasks[1].id)
    done = board.update_task(bob, tasks[1].id, status="done", commit="9F3C2A1b")
    assert done.commit == "9f3c2a1b"
    assert "commit 9f3c2a1b" in format_task(done, clock())
    event = board.events_for(alice, "shop")[-1]
    assert event.kind == "done"
    line = format_event(event, clock())
    assert 'bob finished #2 "Checkout API"' in line and "landed on feature/checkout in 9f3c2a1b" in line


def test_commit_must_look_like_a_sha(board, alice):
    t = board.create_task(alice, "shop", "Thing")
    with pytest.raises(BoardError, match="commit"):
        board.update_task(alice, t.id, status="done", commit="the big one")


def test_hooks_show_plan_progress_and_landed_commits(board, alice, bob):
    tasks = load_prd(board, alice)
    board.claim_task(alice, tasks[0].id)
    start = session_start_context(board, bob, "b1", "/home/bob/code/shop")
    assert "Plans in shop:" in start and "prd-checkout (branch feature/checkout" in start and "0/5 done" in start

    board.update_task(alice, tasks[0].id, status="done", commit="abc1234")
    update = prompt_context(board, bob, "b1", "/home/bob/code/shop")
    assert "landed on feature/checkout in abc1234" in update


def test_board_ignores_finished_plans_in_session_start(board, alice, bob):
    tasks = load_prd(board, alice)
    for t in tasks:
        board.update_task(alice, t.id, status="done")
    assert "Plans in shop:" not in session_start_context(board, bob, "b1", "/code/shop")


def test_existing_databases_gain_new_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(
        """CREATE TABLE tasks (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, title TEXT NOT NULL,
               ref TEXT, link TEXT, status TEXT NOT NULL, claimed_by INTEGER, claimed_at TEXT,
               created_by INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"""
    )
    old.close()
    conn = db.connect(path)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert {"plan_id", "commit_sha"} <= columns


def test_bulk_task_imports_collapse_to_one_line(board, alice, bob):
    session_start_context(board, bob, "b1", "/code/shop")
    load_prd(board, alice)
    update = prompt_context(board, bob, "b1", "/code/shop")
    assert "alice added 5 tasks to plan prd-checkout (#1–#5)" in update
    assert update.count("added task") == 0


def test_active_lists_keep_plan_items_in_document_order(board, clock, alice, bob):
    tasks = load_prd(board, alice)
    clock.advance(minutes=1)
    board.update_task(alice, tasks[3].id, note="touched")  # recent activity must not reorder the plan
    board.claim_task(bob, tasks[2].id)
    loose = board.create_task(alice, "shop", "Unplanned chore", claim=False)
    order = [t.id for t in board.tasks("shop")]
    assert order == [tasks[2].id, tasks[0].id, tasks[1].id, tasks[3].id, tasks[4].id, loose.id]
