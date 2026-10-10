"""Known-good public reference for independent CRUD checks."""
import json
import sys


def apply(operations):
    records, responses, next_id = {}, [], 1
    for operation in operations:
        action = operation.get("op")
        identifier = operation.get("id")
        name = operation.get("name")
        if action not in {"create", "get", "update", "delete"}:
            responses.append({"error": "UNKNOWN_OPERATION"})
        elif action != "create" and identifier not in records:
            responses.append({"error": "NOT_FOUND"})
        elif action in {"create", "update"} and (not isinstance(name, str) or not name.strip()):
            responses.append({"error": "INVALID_NAME"})
        elif action == "create":
            records[next_id] = {"id": next_id, "name": name}
            responses.append(dict(records[next_id]))
            next_id += 1
        elif action == "get":
            responses.append(dict(records[identifier]))
        elif action == "update":
            records[identifier]["name"] = name
            responses.append(dict(records[identifier]))
        else:
            del records[identifier]
            responses.append({"deleted": identifier})
    return responses


if __name__ == "__main__":
    print(json.dumps(apply(json.loads(sys.argv[1])["operations"])))
