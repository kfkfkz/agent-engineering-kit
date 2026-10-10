"""Public batch catalog CLI. Implement the missing CRUD behavior."""
import json
import sys


def apply(operations):
    return [{"error": "UNIMPLEMENTED"} for _operation in operations]


if __name__ == "__main__":
    print(json.dumps(apply(json.loads(sys.argv[1])["operations"])))
