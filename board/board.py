"""Board domain logic: projects, plans, tasks, claims, messages, notes and read cursors.

Pure functions over a SQLite connection; no HTTP. Every state change writes an event,
which is how the other people's Claudes find out about it.
"""

import contextlib
import functools
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

from .auth import User

STALE_AFTER = timedelta(days=7)
ACTIVE = ("open", "claimed")
# Folder names too common to be project names: a project called "docs" would capture every repo's docs folder.
GENERIC_NAMES = frozenset(
    {
        "app", "apps", "bin", "build", "code", "desktop", "dev", "doc", "docs", "documents", "downloads", "git",
        "github", "home", "lib", "packages", "projects", "repos", "scripts", "src", "test", "tests", "tmp",
        "users", "workspace",
    }
)  # fmt: skip


class BoardError(Exception):
    """A user-facing problem (conflict, unknown id...). The message is shown to Claude verbatim."""


@dataclass(frozen=True)
class Task:
    id: int
    project: str
    title: str
    ref: str | None
    link: str | None
    status: str
    claimed_by: str | None
    claimed_at: datetime | None
    created_by: str
    updated_at: datetime
    plan: str | None = None
    commit: str | None = None
    plan_branch: str | None = None

    def is_stale(self, now: datetime) -> bool:
        return self.status == "claimed" and now - self.updated_at > STALE_AFTER


@dataclass(frozen=True)
class Plan:
    """A named group of tasks in a project, typically one PRD or spec, optionally tied to a branch."""

    name: str
    project: str
    doc: str | None
    branch: str | None


@dataclass(frozen=True)
class PlanSummary:
    plan: Plan
    total: int
    done: int
    open: int
    dropped: int
    claimed: dict[str, list[int]]  # person -> task ids, in plan order


@dataclass(frozen=True)
class Event:
    id: int
    project: str | None
    task_id: int | None
    task_title: str | None
    task_commit: str | None
    task_branch: str | None
    task_plan: str | None
    author: str
    kind: str
    body: str
    to_user: str | None
    reply_to: int | None
    needs_ack: bool
    acked_at: datetime | None
    ack_reply: str | None
    created_at: datetime


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _norm(text: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _numbers(normalized: str) -> list[str]:
    return re.findall(r"\d+", normalized)


def _atomic(method):
    """Run a Board method in one transaction, so a failure part-way leaves no partial writes."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.transaction():
            return method(self, *args, **kwargs)

    return wrapper


class Board:
    def __init__(self, conn: sqlite3.Connection, clock=utcnow):
        self.conn = conn
        self.clock = clock
        # Serialises all access to the shared connection across request threads.
        self.lock = threading.RLock()
        self._depth = 0

    @contextlib.contextmanager
    def transaction(self):
        """Reentrant: only the outermost level issues BEGIN/COMMIT (or ROLLBACK on error)."""
        with self.lock:
            if self._depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("COMMIT")

    # ---- users & projects -------------------------------------------------

    def user_named(self, name: str) -> User:
        row = self.conn.execute(
            "SELECT id, name FROM users WHERE name = ? AND active = 1", (name.strip().lower().lstrip("@"),)
        ).fetchone()
        if row is None:
            known = ", ".join(r["name"] for r in self.conn.execute("SELECT name FROM users WHERE active = 1"))
            raise BoardError(f"No person named {name!r} on the board. Known: {known}.")
        return User(row["id"], row["name"])

    def list_projects(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.name, p.aliases,
                      (SELECT COUNT(*) FROM tasks t
                       WHERE t.project_id = p.id AND t.status IN ('open', 'claimed')) AS active
               FROM projects p ORDER BY p.name"""
        ).fetchall()

    def _project_id(self, name: str, create: bool = False) -> int:
        key = name.strip().lower()
        if not key:
            raise BoardError("A project name is required (normally the repository name).")
        for row in self.conn.execute("SELECT id, name, aliases FROM projects"):
            if key == row["name"] or key in _aliases(row["aliases"]):
                return row["id"]
        if not create:
            known = ", ".join(r["name"] for r in self.list_projects()) or "none yet"
            raise BoardError(f"No project named {name!r}. Known projects: {known}.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", key):
            raise BoardError("Project names are lowercase letters, digits, '.', '-' and '_' (use the repo name).")
        if key in GENERIC_NAMES:
            raise BoardError(f"'{key}' is too generic a folder name for a project; use the repository name.")
        return self.conn.execute(
            "INSERT INTO projects (name, created_at) VALUES (?, ?)", (key, _ts(self.clock()))
        ).lastrowid

    @_atomic
    def add_project_alias(self, project: str, alias: str) -> str:
        pid = self._project_id(project)
        alias = alias.strip().lower()
        if not alias or "," in alias:
            raise BoardError("Aliases are single folder names without commas.")
        if alias in GENERIC_NAMES:
            raise BoardError(f"'{alias}' is too generic a folder name to alias a project.")
        row = self.conn.execute("SELECT name, aliases FROM projects WHERE id = ?", (pid,)).fetchone()
        aliases = _aliases(row["aliases"])
        if alias not in aliases and alias != row["name"]:
            aliases.append(alias)
            self.conn.execute("UPDATE projects SET aliases = ? WHERE id = ?", (",".join(aliases), pid))
        return row["name"]

    def project_for_path(self, path: str | None) -> str | None:
        """The project whose name or alias matches the deepest segment of a working-directory path."""
        segments = [s.lower() for s in re.split(r"[\\/]+", path or "") if s]
        projects = {}
        for row in self.conn.execute("SELECT name, aliases FROM projects"):
            for key in [row["name"], *_aliases(row["aliases"])]:
                projects[key] = row["name"]
        for segment in reversed(segments):
            if segment in projects:
                return projects[segment]
        return None

    # ---- plans ------------------------------------------------------------

    @_atomic
    def define_plan(
        self, user: User, project: str, name: str, doc: str | None = None, branch: str | None = None
    ) -> Plan:
        """Create a plan, or update its doc/branch (fields left as None keep their value)."""
        key = (name or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", key):
            raise BoardError("Plan names are lowercase letters, digits, '.', '-' and '_', e.g. 'prd-checkout'.")
        pid = self._project_id(project, create=True)
        doc, branch = _clean(doc), _clean(branch)
        row = self.conn.execute("SELECT id FROM plans WHERE project_id = ? AND name = ?", (pid, key)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO plans (project_id, name, doc, branch, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (pid, key, doc, branch, user.id, _ts(self.clock())),
            )
        else:
            self.conn.execute(
                "UPDATE plans SET doc = COALESCE(?, doc), branch = COALESCE(?, branch) WHERE id = ?",
                (doc, branch, row["id"]),
            )
        return self.get_plan(project, key)

    def get_plan(self, project: str, name: str) -> Plan:
        pid = self._project_id(project)
        row = self.conn.execute(
            _PLAN_SELECT + " WHERE pl.project_id = ? AND pl.name = ?", (pid, name.strip().lower())
        ).fetchone()
        if row is None:
            known = ", ".join(p.name for p in self.plans(project)) or "none yet"
            raise BoardError(
                f"No plan named {name!r} in {self._project_name(pid)}. Known plans: {known}. "
                "Create it with define_plan."
            )
        return _plan(row)

    def plans(self, project: str, active_only: bool = False) -> list[Plan]:
        sql = _PLAN_SELECT + " WHERE pl.project_id = ?"
        if active_only:
            sql += " AND EXISTS (SELECT 1 FROM tasks t WHERE t.plan_id = pl.id AND t.status IN ('open', 'claimed'))"
        return [_plan(r) for r in self.conn.execute(sql + " ORDER BY pl.id", (self._project_id(project),))]

    def plan_summary(self, project: str, name: str) -> PlanSummary:
        plan = self.get_plan(project, name)
        tasks = self.tasks(project, plan=plan.name, statuses=("open", "claimed", "done", "dropped"))
        claimed: dict[str, list[int]] = {}
        for t in tasks:
            if t.status == "claimed":
                claimed.setdefault(t.claimed_by, []).append(t.id)
        count = lambda status: sum(1 for t in tasks if t.status == status)  # noqa: E731
        return PlanSummary(plan, len(tasks), count("done"), count("open"), count("dropped"), claimed)

    def _project_name(self, project_id: int) -> str:
        return self.conn.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()[0]

    # ---- tasks ------------------------------------------------------------

    def get_task(self, task_id: int) -> Task:
        row = self.conn.execute(_TASK_SELECT + " WHERE t.id = ?", (task_id,)).fetchone()
        if row is None:
            raise BoardError(f"No task #{task_id}.")
        return _task(row)

    def tasks(
        self, project: str | None = None, statuses=ACTIVE, query: str | None = None, plan: str | None = None
    ) -> list[Task]:
        """Matching tasks: claimed first, then plan items in document order, then the rest by recent activity."""
        placeholders = ",".join("?" * len(statuses))
        sql, args = f"{_TASK_SELECT} WHERE t.status IN ({placeholders})", list(statuses)
        if project:
            sql += " AND t.project_id = ?"
            args.append(self._project_id(project))
        if plan:
            if not project:
                raise BoardError("A plan filter needs the project too.")
            sql += " AND pl.name = ?"
            args.append(self.get_plan(project, plan).name)
            sql += " ORDER BY t.id"
        else:
            # Claimed first; then plan items in document order; then other tasks by recent activity.
            sql += " ORDER BY t.status = 'claimed' DESC, t.plan_id IS NULL, t.plan_id, "
            sql += "CASE WHEN t.plan_id IS NOT NULL THEN t.id END, t.updated_at DESC"
        found = [_task(r) for r in self.conn.execute(sql, args)]
        if query:
            q = _norm(query)
            found = [t for t in found if q in _norm(t.title) or q in _norm(t.ref) or q in _norm(t.link)]
        return found

    def find_duplicates(self, project: str, title: str, ref: str | None) -> list[Task]:
        """Active tasks in the project with the same doc reference or a near-identical title."""
        try:
            candidates = self.tasks(project)
        except BoardError:
            return []
        n_title, n_ref = _norm(title), _norm(ref)
        dupes = []
        for t in candidates:
            t_ref, t_title = _norm(t.ref), _norm(t.title)
            if n_ref and t_ref:
                # Both point at a document section: the refs decide ("item 8" vs "item 9" are siblings).
                if n_ref == t_ref:
                    dupes.append(t)
                continue
            # Titles that differ only in a number ("bug #12" / "bug #13") are different tasks.
            if _numbers(n_title) == _numbers(t_title) and SequenceMatcher(None, n_title, t_title).ratio() >= 0.85:
                dupes.append(t)
        return dupes

    @_atomic
    def create_task(
        self,
        user: User,
        project: str,
        title: str,
        ref: str | None = None,
        link: str | None = None,
        claim: bool = True,
        force: bool = False,
        plan: str | None = None,
    ) -> Task:
        title, ref, link = (title or "").strip(), _clean(ref), _clean(link)
        if not title:
            raise BoardError("A task needs a title.")
        plan_id = None
        if plan:
            pid_for_plan = self._project_id(project)  # a plan's project already exists
            plan_id = self.conn.execute(
                "SELECT id FROM plans WHERE project_id = ? AND name = ?",
                (pid_for_plan, self.get_plan(project, plan).name),
            ).fetchone()["id"]
        if not force:
            dupes = self.find_duplicates(project, title, ref)
            if dupes:
                lines = "\n".join(format_task(t, self.clock()) for t in dupes)
                raise BoardError(
                    "Not created: this looks like an existing task. Claim or update it instead, "
                    f"or call again with force=true if it really is different.\n{lines}"
                )
        pid = self._project_id(project, create=True)
        now = _ts(self.clock())
        status = "claimed" if claim else "open"
        task_id = self.conn.execute(
            """INSERT INTO tasks (project_id, title, ref, link, status, claimed_by, claimed_at,
                                  created_by, created_at, updated_at, plan_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pid,
                title,
                ref,
                link,
                status,
                user.id if claim else None,
                now if claim else None,
                user.id,
                now,
                now,
                plan_id,
            ),
        ).lastrowid
        self._event(user, "claimed" if claim else "task_created", project_id=pid, task_id=task_id)
        return self.get_task(task_id)

    @_atomic
    def claim_task(self, user: User, task_id: int, take_over: bool = False) -> Task:
        task = self.get_task(task_id)
        now = self.clock()
        if task.status in ("done", "dropped"):
            raise BoardError(f"Task #{task_id} is already {task.status}. Reopen it with update_task(status='open').")
        if task.status == "claimed" and task.claimed_by == user.name:
            return self.update_task(user, task_id)
        if task.status == "claimed":
            since = _ago(task.claimed_at, now)
            if not task.is_stale(now):
                raise BoardError(
                    f"Task #{task_id} is claimed by {task.claimed_by} (since {since}, last activity "
                    f"{_ago(task.updated_at, now)}). Pick something else or coordinate with them."
                )
            if not take_over:
                raise BoardError(
                    f"Task #{task_id} is claimed by {task.claimed_by} but has been idle since "
                    f"{_ago(task.updated_at, now)}. Call again with take_over=true to take it over."
                )
        self.conn.execute(
            "UPDATE tasks SET status = 'claimed', claimed_by = ?, claimed_at = ?, updated_at = ? WHERE id = ?",
            (user.id, _ts(now), _ts(now), task_id),
        )
        body = f"took over from {task.claimed_by}" if task.status == "claimed" else ""
        self._event(user, "claimed", project_name=task.project, task_id=task_id, body=body)
        return self.get_task(task_id)

    @_atomic
    def update_task(
        self,
        user: User,
        task_id: int,
        note: str | None = None,
        link: str | None = None,
        status: str | None = None,
        commit: str | None = None,
    ) -> Task:
        task = self.get_task(task_id)
        note, link, commit = _clean(note), _clean(link), _clean(commit)
        if commit:
            commit = commit.lower()
            if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
                raise BoardError("commit must be a git commit hash (7-40 hex characters), e.g. 'abc1234'.")
        now = _ts(self.clock())
        if status is not None and status not in ("open", "done", "dropped"):
            raise BoardError("status must be 'open' (release / reopen), 'done' or 'dropped'.")
        held_by_other = task.status == "claimed" and task.claimed_by != user.name and not task.is_stale(self.clock())
        if status and held_by_other:
            verb = {"open": "release", "done": "mark it done", "dropped": "drop"}[status]
            raise BoardError(
                f"Task #{task_id} is claimed by {task.claimed_by}; only they can {verb} it until the claim goes "
                "stale. A progress note (update_task with just `note`) is fine."
            )
        if link:
            self.conn.execute("UPDATE tasks SET link = ? WHERE id = ?", (link, task_id))
        if commit:
            self.conn.execute("UPDATE tasks SET commit_sha = ? WHERE id = ?", (commit, task_id))
        if status == "open":
            self.conn.execute(
                "UPDATE tasks SET status = 'open', claimed_by = NULL, claimed_at = NULL WHERE id = ?", (task_id,)
            )
        elif status:
            self.conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        self.conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (now, task_id))

        kind = {"open": "released", "done": "done", "dropped": "dropped"}.get(status or "", "note")
        body = " · ".join(filter(None, [note, f"link: {link}" if link else None]))
        if commit and kind != "done":  # a done event shows the task's commit itself
            body = " · ".join(filter(None, [body, f"commit {commit}"]))
        if kind != "note" or body:
            self._event(user, kind, project_name=task.project, task_id=task_id, body=body)
        return self.get_task(task_id)

    # ---- messages & notes -------------------------------------------------

    @_atomic
    def post_message(self, user: User, text: str, project: str | None = None, to: str | None = None) -> Event:
        text = (text or "").strip()
        if not text:
            raise BoardError("The message is empty.")
        recipient = self.user_named(to) if to else None
        if recipient and recipient.id == user.id:
            raise BoardError("That note is addressed to yourself.")
        pid = self._project_id(project) if project else None
        event_id = self._event(
            user,
            "note" if recipient else "message",
            project_id=pid,
            body=text,
            to_user_id=recipient.id if recipient else None,
            needs_ack=recipient is not None,
        )
        return self.get_event(event_id)

    @_atomic
    def ack_note(self, user: User, note_id: int, reply: str) -> Event:
        note = self.get_event(note_id)
        if note.to_user != user.name or not note.needs_ack:
            raise BoardError(f"Event #{note_id} is not a note addressed to you.")
        if note.acked_at:
            raise BoardError(f"Note #{note_id} was already acknowledged.")
        reply = (reply or "").strip() or "acknowledged"
        self.conn.execute(
            "UPDATE events SET acked_at = ?, ack_reply = ? WHERE id = ?", (_ts(self.clock()), reply, note_id)
        )
        row = self.conn.execute("SELECT author_id, project_id FROM events WHERE id = ?", (note_id,)).fetchone()
        event_id = self._event(
            user, "ack", project_id=row["project_id"], body=reply, to_user_id=row["author_id"], reply_to=note_id
        )
        return self.get_event(event_id)

    def get_event(self, event_id: int) -> Event:
        row = self.conn.execute(_EVENT_SELECT + " WHERE e.id = ?", (event_id,)).fetchone()
        if row is None:
            raise BoardError(f"No event #{event_id}.")
        return _event(row)

    def unacked_notes(self, user: User) -> list[Event]:
        rows = self.conn.execute(
            _EVENT_SELECT + " WHERE e.to_user_id = ? AND e.needs_ack = 1 AND e.acked_at IS NULL ORDER BY e.id",
            (user.id,),
        )
        return [_event(r) for r in rows]

    def events_for(
        self,
        user: User,
        project: str | None,
        after_id: int = 0,
        limit: int = 50,
        include_own: bool = False,
        all_projects: bool = False,
        oldest_first: bool = False,
    ) -> list[Event]:
        """Events visible to ``user``: public ones for the project (or every project), global ones,
        and those addressed to them. Returned oldest first; ``limit`` keeps the newest ones, or the
        oldest ones with ``oldest_first`` (for paging forward from a cursor)."""
        sql = _EVENT_SELECT + " WHERE e.id > ? AND (e.to_user_id = ? OR (e.to_user_id IS NULL AND ("
        args: list = [after_id, user.id]
        if all_projects:
            sql += "1"
        else:
            sql += "e.project_id IS NULL"
            if project:
                sql += " OR e.project_id = ?"
                args.append(self._project_id(project))
        sql += ")))"
        if not include_own:
            sql += " AND e.author_id != ?"
            args.append(user.id)
        sql += f" ORDER BY e.id {'ASC' if oldest_first else 'DESC'} LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(sql, args).fetchall()
        return [_event(r) for r in (rows if oldest_first else reversed(rows))]

    def latest_event_id(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    # ---- read cursors (used by hooks) ------------------------------------

    # Keys are "session:<id>" (what that session has been shown) and "project:<name>" (what the user has
    # been shown about that project across sessions; "project:" covers folders outside any project).

    def cursor(self, user: User, key: str) -> int | None:
        row = self.conn.execute(
            "SELECT last_event_id FROM cursors WHERE user_id = ? AND session_id = ?", (user.id, key)
        ).fetchone()
        return row[0] if row else None

    def set_cursor(self, user: User, key: str, event_id: int) -> None:
        """Advance a cursor (never moves backwards)."""
        self.conn.execute(
            """INSERT INTO cursors (user_id, session_id, last_event_id, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (user_id, session_id) DO UPDATE SET
                   last_event_id = MAX(last_event_id, excluded.last_event_id), updated_at = excluded.updated_at""",
            (user.id, key, event_id, _ts(self.clock())),
        )

    # ---- internals ------------------------------------------------------

    def _event(
        self,
        user: User,
        kind: str,
        *,
        project_id: int | None = None,
        project_name: str | None = None,
        task_id: int | None = None,
        body: str = "",
        to_user_id: int | None = None,
        reply_to: int | None = None,
        needs_ack: bool = False,
    ) -> int:
        if project_name and project_id is None:
            project_id = self._project_id(project_name)
        return self.conn.execute(
            """INSERT INTO events (project_id, task_id, author_id, kind, body, to_user_id, reply_to, needs_ack,
                                   created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, task_id, user.id, kind, body, to_user_id, reply_to, int(needs_ack), _ts(self.clock())),
        ).lastrowid


# ---- formatting (shared by MCP tools and hooks) ------------------------------


def format_task(t: Task, now: datetime) -> str:
    if t.status == "claimed":
        state = f"claimed by {t.claimed_by} {_ago(t.claimed_at, now)}"
        if t.is_stale(now):
            state += f", STALE (idle since {_ago(t.updated_at, now)})"
    else:
        state = t.status
    extras = "".join(
        [
            f" · plan: {t.plan}" if t.plan else "",
            f" · ref: {t.ref}" if t.ref else "",
            f" · {t.link}" if t.link else "",
            f" · commit {t.commit}" if t.commit else "",
        ]
    )
    return f"#{t.id} [{t.project}] {t.title} — {state}{extras}"


def format_plan_summary(s: PlanSummary) -> str:
    where = ", ".join(filter(None, [f"branch {s.plan.branch}" if s.plan.branch else None,
                                    f"doc {s.plan.doc}" if s.plan.doc else None]))  # fmt: skip
    parts = [f"{s.done}/{s.total - s.dropped} done"]
    if s.claimed:
        holders = "; ".join(f"{who} {' '.join(f'#{i}' for i in ids)}" for who, ids in sorted(s.claimed.items()))
        parts.append(f"claimed: {holders}")
    parts.append(f"{s.open} open")
    return f"{s.plan.name}{f' ({where})' if where else ''}: {' · '.join(parts)}"


_EVENT_VERBS = {
    "task_created": "added task",
    "claimed": "claimed",
    "released": "released",
    "done": "finished",
    "dropped": "dropped",
    "note": "progress note on",
}


def format_event(e: Event, now: datetime) -> str:
    when = _ago(e.created_at, now)
    where = f"[{e.project}] " if e.project else "[global] "
    if e.kind in _EVENT_VERBS and e.task_id:
        text = f'{e.author} {_EVENT_VERBS[e.kind]} #{e.task_id} "{e.task_title}"'
        if e.kind == "done" and e.task_commit:
            text += f" — landed {f'on {e.task_branch} ' if e.task_branch else ''}in {e.task_commit}"
        if e.body:
            text += f": {e.body}"
    elif e.kind == "message":
        text = f"{e.author} posted: {e.body}"
    elif e.kind == "note":
        status = f"acknowledged: {e.ack_reply}" if e.acked_at else "awaiting acknowledgement"
        text = f"note #{e.id} from {e.author} to {e.to_user} ({status}): {e.body}"
    elif e.kind == "ack":
        text = f"{e.author} acknowledged note #{e.reply_to}: {e.body}"
    else:
        text = f"{e.author} {e.kind}: {e.body}"
    return f"{where}{text} ({when})"


def _ago(then: datetime | None, now: datetime) -> str:
    if then is None:
        return "?"
    seconds = int((now - then).total_seconds())
    if seconds < 60:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def _aliases(raw: str) -> list[str]:
    return [a for a in raw.split(",") if a]


_TASK_SELECT = """
SELECT t.*, p.name AS project, cu.name AS claimed_by_name, au.name AS created_by_name,
       pl.name AS plan_name, pl.branch AS plan_branch
FROM tasks t
JOIN projects p ON p.id = t.project_id
JOIN users au ON au.id = t.created_by
LEFT JOIN users cu ON cu.id = t.claimed_by
LEFT JOIN plans pl ON pl.id = t.plan_id
"""

_EVENT_SELECT = """
SELECT e.*, p.name AS project, t.title AS task_title, t.commit_sha AS task_commit,
       pl.branch AS task_branch, pl.name AS task_plan, au.name AS author, tu.name AS to_user
FROM events e
JOIN users au ON au.id = e.author_id
LEFT JOIN projects p ON p.id = e.project_id
LEFT JOIN tasks t ON t.id = e.task_id
LEFT JOIN plans pl ON pl.id = t.plan_id
LEFT JOIN users tu ON tu.id = e.to_user_id
"""

_PLAN_SELECT = """
SELECT pl.*, p.name AS project FROM plans pl JOIN projects p ON p.id = pl.project_id
"""


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        project=row["project"],
        title=row["title"],
        ref=row["ref"],
        link=row["link"],
        status=row["status"],
        claimed_by=row["claimed_by_name"],
        claimed_at=_dt(row["claimed_at"]),
        created_by=row["created_by_name"],
        updated_at=_dt(row["updated_at"]),
        plan=row["plan_name"],
        commit=row["commit_sha"],
        plan_branch=row["plan_branch"],
    )


def _plan(row: sqlite3.Row) -> Plan:
    return Plan(name=row["name"], project=row["project"], doc=row["doc"], branch=row["branch"])


def _event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        project=row["project"],
        task_id=row["task_id"],
        task_title=row["task_title"],
        task_commit=row["task_commit"],
        task_branch=row["task_branch"],
        task_plan=row["task_plan"],
        author=row["author"],
        kind=row["kind"],
        body=row["body"],
        to_user=row["to_user"],
        reply_to=row["reply_to"],
        needs_ack=bool(row["needs_ack"]),
        acked_at=_dt(row["acked_at"]),
        ack_reply=row["ack_reply"],
        created_at=_dt(row["created_at"]),
    )
