"""A destructive baseline migration breaks historical data and compatibility."""
import json
import sqlite3
import sys


def migrate(request):
    with sqlite3.connect(request["db"]) as connection:
        connection.execute("DROP TABLE IF EXISTS users")
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, display_name TEXT NOT NULL)")
        connection.execute("PRAGMA user_version=2")
    return {"version": 2}


if __name__ == "__main__":
    print(json.dumps(migrate(json.loads(sys.argv[1]))))
