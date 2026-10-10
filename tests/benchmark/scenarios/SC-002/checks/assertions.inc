def verify(root):
    expect(root, {"operations": [
        {"op": "create", "name": "alpha"}, {"op": "get", "id": 1},
        {"op": "update", "id": 1, "name": "beta"}, {"op": "get", "id": 1},
        {"op": "delete", "id": 1}, {"op": "get", "id": 1},
    ]}, [{"id": 1, "name": "alpha"}, {"id": 1, "name": "alpha"},
         {"id": 1, "name": "beta"}, {"id": 1, "name": "beta"},
         {"deleted": 1}, {"error": "NOT_FOUND"}])
    expect(root, {"operations": [
        {"op": "create", "name": " "}, {"op": "create", "name": False},
        {"op": "create", "name": "kept"}, {"op": "update", "id": 1, "name": ""},
        {"op": "get", "id": 1}, {"op": "delete", "id": 99}, {"op": "unexpected"},
        {"op": "create", "name": "second"},
    ]}, [{"error": "INVALID_NAME"}, {"error": "INVALID_NAME"},
         {"id": 1, "name": "kept"}, {"error": "INVALID_NAME"}, {"id": 1, "name": "kept"},
         {"error": "NOT_FOUND"}, {"error": "UNKNOWN_OPERATION"}, {"id": 2, "name": "second"}])
    expect(root, {"operations": []}, [])


checked_main(verify)
