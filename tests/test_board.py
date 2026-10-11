import pytest

from board import auth
from board.board import BoardError, format_event, format_task


def test_tokens_are_hashed_and_resolve(conn, tokens):
    assert auth.user_for_token(conn, tokens["alice"]).name == "alice"
    assert auth.user_for_token(conn, "cb_wrong") is None
    stored = [r[0] for r in conn.execute("SELECT token_hash FROM users")]
    assert tokens["alice"] not in stored


def test_rotate_token_invalidates_old(conn, tokens):
    new = auth.rotate_token(conn, "alice")
    assert auth.user_for_token(conn, tokens["alice"]) is None
    assert auth.user_for_token(conn, new).name == "alice"


def test_create_task_claims_and_creates_project(board, alice):
    task = board.create_task(alice, "Media-Hub", "Add series support", ref="Spec XYZ item 8")
    assert task.project == "media-hub"
    assert task.status == "claimed" and task.claimed_by == "alice"
    assert [p["name"] for p in board.list_projects()] == ["media-hub"]


def test_duplicate_by_ref_or_title_is_refused(board, alice, bob):
    board.create_task(alice, "shop", "Add series support", ref="Spec XYZ item 8")
    with pytest.raises(BoardError, match="existing task"):
        board.create_task(bob, "shop", "Something worded differently", ref="spec xyz, item 8")
    with pytest.raises(BoardError, match="existing task"):
        board.create_task(bob, "shop", "add series support!")
    assert board.create_task(bob, "shop", "add series support", force=True).id == 2
    # Unrelated work is fine.
    assert board.create_task(bob, "shop", "Fix login redirect").status == "claimed"


def test_done_tasks_do_not_count_as_duplicates(board, alice):
    t = board.create_task(alice, "shop", "Add series support")
    board.update_task(alice, t.id, status="done")
    assert board.create_task(alice, "shop", "Add series support").id == 2


def test_claim_conflict_names_holder(board, alice, bob):
    t = board.create_task(alice, "shop", "Refactor auth")
    with pytest.raises(BoardError, match="claimed by alice"):
        board.claim_task(bob, t.id)


def test_stale_claim_can_be_taken_over(board, clock, alice, bob):
    t = board.create_task(alice, "shop", "Refactor auth")
    clock.advance(days=8)
    assert "STALE" in format_task(board.get_task(t.id), clock())
    with pytest.raises(BoardError, match="take_over=true"):
        board.claim_task(bob, t.id)
    taken = board.claim_task(bob, t.id, take_over=True)
    assert taken.claimed_by == "bob"
    last = board.events_for(alice, "shop")[-1]
    assert last.kind == "claimed" and "took over from alice" in last.body


def test_activity_keeps_claim_fresh(board, clock, alice, bob):
    t = board.create_task(alice, "shop", "Refactor auth")
    clock.advance(days=6)
    board.update_task(alice, t.id, note="still on it")
    clock.advance(days=6)
    with pytest.raises(BoardError, match="claimed by alice"):
        board.claim_task(bob, t.id, take_over=True)


def test_release_and_reclaim(board, alice, bob):
    t = board.create_task(alice, "shop", "Refactor auth")
    with pytest.raises(BoardError, match="only they can release"):
        board.update_task(bob, t.id, status="open")
    board.update_task(alice, t.id, status="open")
    assert board.claim_task(bob, t.id).claimed_by == "bob"


def test_update_link_and_done_write_events(board, alice, bob):
    t = board.create_task(alice, "shop", "Refactor auth")
    board.update_task(alice, t.id, note="PR opened", link="https://github.com/x/app/pull/12")
    board.update_task(alice, t.id, status="done")
    events = board.events_for(bob, "shop")
    assert [e.kind for e in events] == ["claimed", "note", "done"]
    assert "pull/12" in board.get_task(t.id).link
    assert "PR opened" in format_event(events[1], board.clock())


def test_find_tasks_searches_title_ref_and_link(board, alice):
    board.create_task(alice, "shop", "Add series support", ref="docs/specs/xyz.md §8")
    board.create_task(alice, "shop", "Fix login")
    assert [t.title for t in board.tasks(query="xyz md")] == ["Add series support"]
    assert [t.title for t in board.tasks(query="LOGIN")] == ["Fix login"]


def test_directed_note_ack_round_trip(board, alice, bob):
    note = board.post_message(alice, "Install the codeindex MCP: claude mcp add ...", to="Bob")
    assert note.needs_ack and note.to_user == "bob" and note.project is None
    assert [n.id for n in board.unacked_notes(bob)] == [note.id]
    with pytest.raises(BoardError, match="not a note addressed to you"):
        board.ack_note(alice, note.id, "done")
    ack = board.ack_note(bob, note.id, "installed; search works")
    assert board.unacked_notes(bob) == []
    assert ack.to_user == "alice" and ack.reply_to == note.id
    assert any(e.kind == "ack" for e in board.events_for(alice, None))
    with pytest.raises(BoardError, match="already acknowledged"):
        board.ack_note(bob, note.id, "again")


def test_notes_are_private_to_recipient(board, conn, alice, bob):
    auth.create_user(conn, "guest")
    guest = auth.user_for_token(conn, auth.rotate_token(conn, "guest"))
    board.post_message(alice, "for bob only", to="bob")
    assert board.events_for(guest, None, all_projects=True) == []
    assert len(board.events_for(bob, None)) == 1


def test_unknown_recipient(board, alice):
    with pytest.raises(BoardError, match="Known: "):
        board.post_message(alice, "hi", to="carol")


def test_events_scope_by_project(board, alice, bob):
    board.create_task(alice, "shop", "A")
    board.create_task(alice, "other", "B")
    board.post_message(alice, "global hello")
    kinds = [(e.project, e.kind) for e in board.events_for(bob, "shop")]
    assert kinds == [("shop", "claimed"), (None, "message")]
    assert len(board.events_for(bob, None, all_projects=True)) == 3
    assert board.events_for(alice, "shop") == []  # own events excluded by default


def test_project_for_path(board, alice):
    board.create_task(alice, "media-hub", "A")
    board.add_project_alias("media-hub", "mediahub-wt")
    assert board.project_for_path(r"C:\Users\alice\media-hub") == "media-hub"
    assert board.project_for_path("/home/bob/code/MediaHub-WT/src") == "media-hub"
    assert board.project_for_path("/home/bob/elsewhere") is None
    assert board.project_for_path(None) is None


def test_cursors_never_move_backwards(board, alice):
    assert board.cursor(alice, "session:s1") is None
    board.set_cursor(alice, "session:s1", 5)
    board.set_cursor(alice, "session:s1", 3)
    assert board.cursor(alice, "session:s1") == 5
    assert board.cursor(alice, "session:other") is None


def test_project_name_validation(board, alice):
    with pytest.raises(BoardError, match="lowercase"):
        board.create_task(alice, "my project!", "x")
    with pytest.raises(BoardError, match="No project named"):
        board.tasks("missing")


# ---- review fixes -----------------------------------------------------------


def test_sibling_tasks_with_different_refs_or_numbers_are_not_duplicates(board, alice):
    board.create_task(alice, "shop", "Implement spec XYZ item 8", ref="Spec XYZ item 8")
    assert board.create_task(alice, "shop", "Implement spec XYZ item 9", ref="Spec XYZ item 9").id == 2
    board.create_task(alice, "shop", "Fix bug #12")
    assert board.create_task(alice, "shop", "Fix bug #13").id == 4


def test_only_claim_holder_can_finish_or_drop_a_fresh_claim(board, clock, alice, bob):
    t = board.create_task(alice, "shop", "Refactor auth")
    for status in ("done", "dropped", "open"):
        with pytest.raises(BoardError, match="claimed by alice"):
            board.update_task(bob, t.id, status=status)
    assert board.update_task(bob, t.id, note="FYI the API changed").status == "claimed"
    clock.advance(days=8)
    assert board.update_task(bob, t.id, status="done").status == "done"


def test_failed_operations_leave_no_partial_writes(board, conn, alice, bob):
    note = board.post_message(alice, "install codeindex", to="bob")
    conn.execute("UPDATE users SET active = 0 WHERE name = 'alice'")
    ack = board.ack_note(bob, note.id, "done")  # sender deactivated: still delivered
    assert ack.to_user == "alice"
    events_before = board.latest_event_id()
    with pytest.raises(BoardError):
        board.update_task(bob, 999, status="done")
    assert board.latest_event_id() == events_before


def test_messages_need_an_existing_project(board, alice):
    with pytest.raises(BoardError, match="No project named"):
        board.post_message(alice, "hello", project="typo-repo")
    assert board.list_projects() == []


def test_generic_folder_names_are_not_project_names(board, alice):
    with pytest.raises(BoardError, match="generic"):
        board.create_task(alice, "docs", "x")


def test_events_oldest_first_for_paging(board, alice, bob):
    for i in range(5):
        board.post_message(alice, f"m{i}")
    page = board.events_for(bob, None, limit=2, oldest_first=True)
    assert [e.body for e in page] == ["m0", "m1"]
