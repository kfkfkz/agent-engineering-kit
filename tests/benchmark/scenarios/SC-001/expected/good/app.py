"""Known-good public reference, used only to validate the independent verifier."""
import json
import sys


def page(items, offset, limit):
    return items[offset:offset + limit]


if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    print(json.dumps(page(request["items"], request["offset"], request["limit"])))
