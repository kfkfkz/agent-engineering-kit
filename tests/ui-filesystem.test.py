from __future__ import annotations

import os
import ctypes
import queue
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aek.adapters import ui_filesystem as fs


class UiFilesystemTests(unittest.TestCase):
    def test_windows_rename_abi_uses_absolute_destination_and_null_root(self):
        destination = "\\\\?\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\材料\\image-😀.png"
        pointer_size = struct.calcsize("P")
        root_offset = 8 if pointer_size == 8 else 4
        length_offset = root_offset + pointer_size
        name_offset = length_offset + 4
        for overwrite in (False, True):
            with self.subTest(overwrite=overwrite):
                raw = bytes(fs._windows_rename_buffer(destination, overwrite))
                self.assertEqual(raw[0], int(overwrite))
                self.assertEqual(raw[root_offset:length_offset], bytes(pointer_size))
                length = struct.unpack_from("<I", raw, length_offset)[0]
                self.assertEqual(length, len(destination.encode("utf-16-le")))
                self.assertEqual(raw[name_offset:name_offset + length].decode("utf-16-le"), destination)
                self.assertEqual(raw[name_offset + length:name_offset + length + 2], b"\0\0")
                self.assertGreaterEqual(len(raw), ctypes.sizeof(fs._Rename))

    def test_open_file_reads_regular_file_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = fs.open_absolute_dir(Path(directory))
            try:
                fd = fs.open_file(root, "image.png", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                os.write(fd, b"image")
                os.close(fd)
                self.assertEqual(fs.read_at(root, "image.png", 10), b"image")
                for name in ("../image.png", ".", "..", "a/b", "a\\b", "a\0b", "a:stream", "NUL", "trailing."):
                    with self.subTest(name=name), self.assertRaises(ValueError):
                        fs.open_file(root, name, os.O_RDONLY)
            finally:
                fs.close_dir(root)

    def test_size_limit_and_nonregular_placeholders_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = fs.open_absolute_dir(Path(directory))
            try:
                (Path(directory) / "large").write_bytes(b"12345")
                with self.assertRaises(ValueError):
                    fs.read_at(root, "large", 4)
                (Path(directory) / "directory").mkdir()
                with self.assertRaises(OSError):
                    fs.open_file(root, "directory", os.O_RDONLY)
                if os.name == "posix":
                    os.mkfifo(Path(directory) / "pipe")
                    with self.assertRaises(OSError):
                        fs.open_file(root, "pipe", os.O_RDONLY)
            finally:
                fs.close_dir(root)

    def test_file_must_remain_stable_throughout_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image"
            path.write_bytes(b"image")
            root = fs.open_absolute_dir(Path(directory))
            fd = fs.open_file(root, "image", os.O_RDONLY)
            try:
                if os.name == "nt":
                    # The opened file denies writers for the read's lifetime.
                    with self.assertRaises(OSError):
                        fs.open_file(root, "image", os.O_WRONLY)
                    self.assertEqual(fs.read_fd(fd, 5), b"image")
                else:
                    real_read = os.read
                    mutated = False

                    def mutate_after_read(current_fd, size):
                        nonlocal mutated
                        data = real_read(current_fd, size)
                        if current_fd == fd and data and not mutated:
                            mutated = True
                            path.write_bytes(b"other")
                            os.utime(path, ns=(1000000000, 1000000000))
                        return data

                    with patch.object(fs.os, "read", side_effect=mutate_after_read):
                        with self.assertRaisesRegex(ValueError, "changed while reading"):
                            fs.read_fd(fd, 5)
            finally:
                os.close(fd)
                fs.close_dir(root)
            # Material is user-editable again when the read ends.
            path.write_bytes(b"new image")
            self.assertEqual(path.read_bytes(), b"new image")

    def test_publication_and_replace_are_atomic_and_do_not_overwrite_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = fs.open_absolute_dir(Path(directory))
            try:
                for name in ("first", "second"):
                    fs.mkdir(root, name)
                fs.rename_noreplace(root, "first", root, "published")
                with self.assertRaises(FileExistsError):
                    fs.rename_noreplace(root, "second", root, "published")
                self.assertTrue((Path(directory) / "second").is_dir())
                (Path(directory) / "old").write_bytes(b"old")
                (Path(directory) / "new").write_bytes(b"new")
                fs.replace(root, "new", root, "old")
                self.assertEqual(fs.read_at(root, "old", 10), b"new")
                self.assertFalse((Path(directory) / "new").exists())
                fs.fsync_dir(root)
            finally:
                fs.close_dir(root)

    def _child(self, code, *args):
        environment = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.Popen([sys.executable, "-u", "-c", code, *map(str, args)],
                                cwd=Path(__file__).resolve().parents[1],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", env=environment)

    def _signal_reader(self, child):
        received = queue.Queue()

        def read():
            try:
                received.put((child.stdout.readline(), None))
            except Exception as error:
                received.put((None, error))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        return received, reader

    def _stop_child(self, child, reader=None):
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        if reader is not None:
            reader.join(timeout=5)
            self.assertFalse(reader.is_alive(), "readiness reader did not exit after child termination")
        return child.communicate(timeout=5)

    def _expect_signal(self, child, expected, *, timeout=15, pending=None):
        received, reader = pending if pending is not None else self._signal_reader(child)
        try:
            value, error = received.get(timeout=timeout)
        except queue.Empty:
            out, err = self._stop_child(child, reader)
            self.fail(f"child timed out after {timeout}s waiting for {expected!r}; "
                      f"returncode={child.returncode}; stdout={out!r}; stderr={err!r}")
        reader.join(timeout=5)
        self.assertFalse(reader.is_alive(), "readiness reader did not exit after its signal")
        if error is not None or value.strip() != expected:
            out, err = self._stop_child(child)
            self.fail(f"child expected {expected!r}, got {value!r}, reader_error={error!r}; "
                      f"returncode={child.returncode}; stdout={out!r}; stderr={err!r}")

    def _finish_child(self, child, *, timeout=15):
        try:
            return child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            out, err = self._stop_child(child)
            self.fail(f"child did not exit within {timeout}s; returncode={child.returncode}; "
                      f"stdout={out!r}; stderr={err!r}")

    def test_independent_processes_have_exactly_one_publication_winner(self):
        code = '''
import os, sys
from pathlib import Path
from aek.adapters import ui_filesystem as fs
root = fs.open_absolute_dir(Path(sys.argv[1]))
try:
    fs.mkdir(root, sys.argv[2])
    print('ready', flush=True)
    sys.stdin.readline()
    try:
        fs.rename_noreplace(root, sys.argv[2], root, 'published')
        print('winner')
    except FileExistsError:
        print('exists')
finally:
    fs.close_dir(root)
'''
        with tempfile.TemporaryDirectory() as directory:
            children = [self._child(code, directory, name) for name in ("one", "two")]
            try:
                for child in children:
                    self._expect_signal(child, "ready")
                for child in children:
                    child.stdin.write("go\n")
                    child.stdin.flush()
                results = [self._finish_child(child) for child in children]
                self.assertEqual(sorted(out.strip() for out, err in results), ["exists", "winner"])
                self.assertEqual([child.returncode for child in children], [0, 0], results)
            finally:
                for child in children:
                    self._stop_child(child)

    def test_lock_serializes_processes_and_releases_after_exit(self):
        code = '''
import sys
from pathlib import Path
from aek.adapters import ui_filesystem as fs
root = fs.open_absolute_dir(Path(sys.argv[1]))
try:
    print('ready', flush=True)
    with fs.exclusive_lock(root):
        print('acquired', flush=True)
finally:
    fs.close_dir(root)
'''
        with tempfile.TemporaryDirectory() as directory:
            root = fs.open_absolute_dir(Path(directory))
            child = None
            pending = None
            try:
                with fs.exclusive_lock(root):
                    child = self._child(code, directory)
                    self._expect_signal(child, "ready")
                    pending = self._signal_reader(child)
                    # The timeout checks the explicitly blocking lock contract;
                    # child readiness is signalled, rather than guessed by sleep.
                    with self.assertRaises(queue.Empty):
                        pending[0].get(timeout=0.25)
                self._expect_signal(child, "acquired", pending=pending)
                self._finish_child(child)
                self.assertEqual(child.returncode, 0)
                with fs.exclusive_lock(root):
                    pass
            finally:
                if child is not None:
                    self._stop_child(child, pending[1] if pending is not None else None)
                fs.close_dir(root)

    def test_lock_is_recoverable_after_owner_process_dies(self):
        holder_code = '''
import sys
from pathlib import Path
from aek.adapters import ui_filesystem as fs
root = fs.open_absolute_dir(Path(sys.argv[1]))
with fs.exclusive_lock(root):
    print('acquired', flush=True)
    sys.stdin.readline()
'''
        successor_code = '''
import os, sys
from pathlib import Path
from aek.adapters import ui_filesystem as fs
if os.name == 'nt':
    original_wait = fs._kernel.WaitForSingleObject
    def waiting(handle, timeout):
        # CreateMutexW has already opened the owner's existing mutex. This
        # retained handle keeps it alive when the owner is terminated.
        print('waiting', flush=True)
        result = original_wait(handle, timeout)
        if result == 0x80:
            print('abandoned', flush=True)
        return result
    fs._kernel.WaitForSingleObject = waiting
else:
    import fcntl
    original_flock = fcntl.flock
    def waiting(handle, operation):
        if operation == fcntl.LOCK_EX:
            print('waiting', flush=True)
        return original_flock(handle, operation)
    fcntl.flock = waiting
root = fs.open_absolute_dir(Path(sys.argv[1]))
try:
    with fs.exclusive_lock(root):
        print('acquired', flush=True)
finally:
    fs.close_dir(root)
'''
        with tempfile.TemporaryDirectory() as directory:
            owner = self._child(holder_code, directory)
            successor = None
            try:
                self._expect_signal(owner, "acquired")
                successor = self._child(successor_code, directory)
                self._expect_signal(successor, "waiting")
                owner.kill()
                self._finish_child(owner)
                out, err = self._finish_child(successor)
                self.assertEqual(successor.returncode, 0, err)
                expected = ["abandoned", "acquired"] if os.name == "nt" else ["acquired"]
                self.assertEqual(out.splitlines(), expected)
            finally:
                for child in (owner, successor):
                    if child is not None:
                        self._stop_child(child)

    def test_open_chain_cannot_be_redirected_by_ancestor_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "ancestor/child").mkdir(parents=True)
            (base / "outside").mkdir()
            root = fs.open_absolute_dir(base)
            ancestor = fs.open_child_dir(root, "ancestor")
            child = fs.open_child_dir(ancestor, "child")
            fs.close_dir(ancestor)
            fs.close_dir(root)
            try:
                if os.name == "nt":
                    with self.assertRaises(OSError):
                        (base / "ancestor").rename(base / "moved")
                    target = base / "ancestor/child"
                else:
                    (base / "ancestor").rename(base / "moved")
                    (base / "ancestor").symlink_to(base / "outside", target_is_directory=True)
                    target = base / "moved/child"
                fs.mkdir(child, "safe")
                self.assertTrue((target / "safe").is_dir())
                self.assertFalse((base / "outside/safe").exists())
            finally:
                fs.close_dir(child)
            # Once operations end, Windows users can rename material normally.
            if os.name == "nt":
                (base / "ancestor").rename(base / "moved")

    def test_symlink_or_junction_is_never_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "outside").mkdir()
            (base / "outside/secret").write_bytes(b"outside")
            link = base / "link"
            if os.name == "nt":
                # Junction creation works without symlink privilege/admin.
                result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(base / "outside")],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            else:
                link.symlink_to(base / "outside", target_is_directory=True)
            root = fs.open_absolute_dir(base)
            try:
                with self.assertRaises(OSError):
                    fs.open_child_dir(root, "link")
                with self.assertRaises(OSError):
                    fs.open_absolute_dir(link)
                self.assertTrue(stat.S_ISLNK(fs.stat_at(root, "link").st_mode))
                fs.unlink(root, "link")
                self.assertEqual((base / "outside/secret").read_bytes(), b"outside")
                self.assertFalse(link.exists())
                if os.name != "nt":
                    (base / "image").symlink_to(base / "outside/secret")
                    with self.assertRaises(OSError):
                        fs.open_file(root, "image", os.O_WRONLY | os.O_TRUNC)
                    self.assertEqual((base / "outside/secret").read_bytes(), b"outside")
            finally:
                fs.close_dir(root)

    def test_file_symlink_is_refused_before_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "secret").write_bytes(b"outside")
            try:
                (base / "image").symlink_to(base / "secret")
            except OSError as error:
                if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                    self.skipTest("file symlink fixture requires Windows Developer Mode or symlink privilege")
                raise
            root = fs.open_absolute_dir(base)
            try:
                with self.assertRaises(OSError):
                    fs.open_file(root, "image", os.O_WRONLY | os.O_TRUNC)
                self.assertEqual((base / "secret").read_bytes(), b"outside")
            finally:
                fs.close_dir(root)


if __name__ == "__main__":
    unittest.main()
