"""External-key CLI; normalization currently breaks an existing integration."""
import json
import sys

if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    print(json.dumps({"key": request["key"].strip().lower()}))
