# Appended after verify_protocol.py; this is a template fragment, not an importable module.
def verify(root):
    expect(root, {"items": [10, 20, 30, 40], "offset": 0, "limit": 2}, [10, 20])
    expect(root, {"items": [10, 20, 30, 40], "offset": 1, "limit": 3}, [20, 30, 40])
    expect(root, {"items": [10, 20, 30], "offset": 1, "limit": 0}, [])
    expect(root, {"items": [], "offset": 0, "limit": 5}, [])
    expect(root, {"items": [10], "offset": 4, "limit": 5}, [])
    expect(root, {"items": [None, False, {"id": "x"}], "offset": 1, "limit": 9}, [False, {"id": "x"}])


checked_main(verify)
