"""Known-good additive, transactional migration for the synthetic SQLite fixture."""
import json
import sqlite3
import sys


def migrate(request):
    connection = sqlite3.connect(request["db"])
    try:
        connection.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        if "display_name" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
            if request.get("fail_after_schema"):
                connection.rollback()
                return {"error": "INJECTED_FAILURE"}
            connection.execute("UPDATE users SET display_name=name")
        connection.execute("PRAGMA user_version=2")
        connection.commit()
        return {"version": 2}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    print(json.dumps(migrate(json.loads(sys.argv[1]))))
