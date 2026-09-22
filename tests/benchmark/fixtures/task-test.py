"""Controlled task fixture: observable behaviour, not a benchmark assertion."""


def test_atomic_no_replace(rename_no_replace, temporary_directory):
    source = temporary_directory / "source.txt"
    target = temporary_directory / "target.txt"
    source.write_text("original", encoding="utf-8")
    target.write_text("user-owned", encoding="utf-8")
    try:
        rename_no_replace(source, target)
    except FileExistsError:
        pass
    else:
        raise AssertionError("target was overwritten")
    assert source.read_text(encoding="utf-8") == "original"
    assert target.read_text(encoding="utf-8") == "user-owned"
