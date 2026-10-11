from datetime import UTC, datetime, timedelta

import pytest

from board import auth, db
from board.board import Board


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture
def conn():
    return db.connect(":memory:")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def board(conn, clock):
    return Board(conn, clock=clock)


@pytest.fixture
def tokens(conn):
    return {name: auth.create_user(conn, name) for name in ("alice", "bob")}


@pytest.fixture
def alice(conn, tokens):
    return auth.user_for_token(conn, tokens["alice"])


@pytest.fixture
def bob(conn, tokens):
    return auth.user_for_token(conn, tokens["bob"])
