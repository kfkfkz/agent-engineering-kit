import sqlite3
import tempfile


def seed(path):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL)")
        connection.executemany("INSERT INTO users(id,name,email) VALUES (?,?,?)",
                               [(7, "legacy-a", "a@example.invalid"), (19, "legacy-b", "b@example.invalid")])
        connection.execute("PRAGMA user_version=1")


def verify(root):
    with tempfile.TemporaryDirectory(prefix="sqlite-case-", dir=root) as directory:
        database = Path(directory) / "migration.sqlite"
        seed(database)
        expect(root, {"db": str(database)}, {"version": 2})
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT id,name,email,display_name FROM users ORDER BY id").fetchall() == [
                (7, "legacy-a", "a@example.invalid", "legacy-a"),
                (19, "legacy-b", "b@example.invalid", "legacy-b")]
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
            connection.execute("UPDATE users SET display_name='customized' WHERE id=7")
            connection.execute("INSERT INTO users(name,email) VALUES ('new-legacy','new@example.invalid')")
            try:
                connection.execute("INSERT INTO users(name,email) VALUES ('duplicate','a@example.invalid')")
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("legacy uniqueness was lost")
        expect(root, {"db": str(database)}, {"version": 2})
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT display_name FROM users WHERE id=7").fetchone() == ("customized",)
            assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3

        rollback = Path(directory) / "rollback.sqlite"
        seed(rollback)
        expect(root, {"db": str(rollback), "fail_after_schema": True}, {"error": "INJECTED_FAILURE"})
        with sqlite3.connect(rollback) as connection:
            assert [row[1] for row in connection.execute("PRAGMA table_info(users)")] == ["id", "name", "email"]
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
            assert connection.execute("SELECT id,name,email FROM users ORDER BY id").fetchall() == [
                (7, "legacy-a", "a@example.invalid"), (19, "legacy-b", "b@example.invalid")]
        expect(root, {"db": str(rollback)}, {"version": 2})


checked_main(verify)
