"""Controller-only inherited descriptors; no scanner installation or scan."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ProcessDescriptorTests(unittest.TestCase):
    def test_empty_selection_keeps_original_cross_platform_process_behavior(self):
        from aek.adapters.security_process import ProcessPort
        with tempfile.TemporaryDirectory() as directory:
            result = ProcessPort().run((sys.executable, '-c', 'print("smoke")'),
                Path(directory), timeout_seconds=3)
        self.assertEqual(result.reason_code, 'PROCESS_COMPLETED')
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), b'smoke')
        self.assertTrue(result.cleanup_complete)

    def test_invalid_selection_never_starts_a_process(self):
        from aek.adapters.security_process import ProcessPort
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = (None, [], [4], (True,), (False,), (0,), (1,), (2,), (-1,),
                       ('4',), (4.0,), (4, 4), tuple(range(3, 36)), (10 ** 100,))
            for selected in invalid:
                with self.subTest(selected=selected), patch('aek.adapters.security_process.subprocess.Popen') as start:
                    with self.assertRaises(ValueError):
                        ProcessPort().run((sys.executable, '-c', 'pass'), root,
                            timeout_seconds=3, pass_fds=selected)
                    start.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'POSIX descriptor execution only')
    def test_closed_descriptor_is_rejected_and_not_silently_ignored(self):
        from aek.adapters.security_process import ProcessPort
        with tempfile.TemporaryDirectory() as directory:
            with tempfile.TemporaryFile() as file:
                closed = os.dup(file.fileno())
            os.close(closed)
            with patch('aek.adapters.security_process.subprocess.Popen') as start:
                with self.assertRaisesRegex(ValueError, 'INVALID_PROCESS_DESCRIPTORS'):
                    ProcessPort().run((sys.executable, '-c', 'pass'), Path(directory),
                        timeout_seconds=3, pass_fds=(closed,))
                start.assert_not_called()

    def test_non_posix_selection_is_unavailable_before_start(self):
        from aek.adapters.security_process import ProcessPort
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('aek.adapters.security_process.os.name', 'nt'), \
                    patch('aek.adapters.security_process.subprocess.Popen') as start:
                with self.assertRaisesRegex(ValueError, 'PROCESS_DESCRIPTOR_BOUNDARY_UNAVAILABLE'):
                    ProcessPort().run((sys.executable, '-c', 'pass'), root,
                        timeout_seconds=3, pass_fds=(3,))
                start.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'POSIX descriptor execution only')
    def test_only_selected_descriptor_reaches_real_child_and_parent_keeps_ownership(self):
        import fcntl
        from aek.adapters.security_process import ProcessPort
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile() as selected, \
                tempfile.TemporaryFile() as other:
            selected.write(b'bounded local smoke')
            selected.flush()
            selected.seek(0)
            # Make the unrelated FD high and inheritable; close_fds must still
            # exclude it. It cannot collide with ordinary interpreter opens.
            extra = fcntl.fcntl(other.fileno(), fcntl.F_DUPFD, 128)
            os.set_inheritable(extra, True)
            try:
                code = ('import os,sys; print(os.read(int(sys.argv[1]),128).decode()); '
                    'exec("try:\\n os.fstat(int(sys.argv[2]))\\n print(\'LEAK\')'
                    '\\nexcept OSError:\\n print(\'CLOSED\')")')
                result = ProcessPort().run((sys.executable, '-c', code,
                    str(selected.fileno()), str(extra)), Path(directory), timeout_seconds=3,
                    pass_fds=(selected.fileno(),))
                self.assertEqual(result.reason_code, 'PROCESS_COMPLETED')
                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertEqual(result.stdout, b'bounded local smoke\nCLOSED\n')
                self.assertTrue(result.cleanup_complete)
                os.fstat(selected.fileno())
                os.fstat(extra)
            finally:
                os.close(extra)


if __name__ == '__main__':
    unittest.main()
