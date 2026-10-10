"""Return a bounded page of input items. The baseline contains an endpoint bug."""
import json
import sys


def page(items, offset, limit):
    return items[offset:offset + limit - 1]


if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    print(json.dumps(page(request["items"], request["offset"], request["limit"])))
