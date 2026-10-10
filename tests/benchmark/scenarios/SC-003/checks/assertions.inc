def verify(root):
    expect(root, {"keys": ["shared"] * 12}, {"accepted": 1, "committed": ["shared"]})
    expect(root, {"keys": ["a", "b", "a", "c", "b", "c", "d", "d"]},
           {"accepted": 4, "committed": ["a", "b", "c", "d"]})
    expect(root, {"keys": ["x", "y", "z"]}, {"accepted": 3, "committed": ["x", "y", "z"]})
    expect(root, {"keys": []}, {"accepted": 0, "committed": []})


checked_main(verify)
