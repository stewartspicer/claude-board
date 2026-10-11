"""Admin CLI, run inside the container:

docker exec claude-board python -m board.admin add-user bob
docker exec claude-board python -m board.admin rotate-token bob
docker exec claude-board python -m board.admin list-users
docker exec claude-board python -m board.admin deactivate bob
"""

import argparse
import sys

from . import auth, db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m board.admin", description="Manage Claude Board users")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("add-user", "create a user and print their token"),
        ("rotate-token", "issue a new token (old one stops working); also reactivates"),
        ("deactivate", "disable a user's token"),
    ):
        sub.add_parser(name, help=help_text).add_argument("name")
    sub.add_parser("list-users", help="list users")
    args = parser.parse_args(argv)
    conn = db.connect()

    try:
        if args.command == "add-user":
            print(auth.create_user(conn, args.name))
        elif args.command == "rotate-token":
            print(auth.rotate_token(conn, args.name))
        elif args.command == "deactivate":
            if conn.execute("UPDATE users SET active = 0 WHERE name = ?", (args.name.lower(),)).rowcount == 0:
                raise ValueError(f"no user named {args.name!r}")
            print(f"deactivated {args.name.lower()}")
        else:
            for row in conn.execute("SELECT name, active, created_at FROM users ORDER BY name"):
                print(f"{row['name']}\t{'active' if row['active'] else 'inactive'}\t{row['created_at']}")
    except (ValueError, conn.IntegrityError) as exc:
        message = "a user with that name already exists" if isinstance(exc, conn.IntegrityError) else str(exc)
        print(f"error: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
