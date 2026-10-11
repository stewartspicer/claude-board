from board import admin, auth, db


def test_admin_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert admin.main(["add-user", "Bob"]) == 0
    token = capsys.readouterr().out.strip()
    assert token.startswith("cb_")
    conn = db.connect()
    assert auth.user_for_token(conn, token).name == "bob"

    assert admin.main(["add-user", "bob"]) == 1
    assert "already exists" in capsys.readouterr().err

    assert admin.main(["deactivate", "bob"]) == 0
    assert auth.user_for_token(conn, token) is None
    assert admin.main(["rotate-token", "bob"]) == 0
    assert auth.user_for_token(conn, capsys.readouterr().out.strip().splitlines()[-1]).name == "bob"

    assert admin.main(["list-users"]) == 0
    assert "bob\tactive" in capsys.readouterr().out
    assert admin.main(["rotate-token", "nobody"]) == 1
