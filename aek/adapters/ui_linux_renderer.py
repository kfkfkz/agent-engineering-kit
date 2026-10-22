"""Opt-in Linux renderer using the original host RuntimeGrant.

The composition root supplies an adopted runtime and its current adoption
validator. Neither serialized plans nor paths in user HTML can enable this
provider. No installation, PATH discovery or privilege fallback happens here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import sys
import threading
import time

from aek.core.artifact.ui_preview import RenderConditions
from aek.core.security.authority import RuntimeGrant, digest, runtime_binding
from aek.adapters.ui_renderer import ProviderUnavailable, RenderSession
from aek.adapters.ui_cdp import CdpError, CdpPipe
from aek.adapters import ui_filesystem as ui_fs

_SYSTEM_ROOTS = frozenset(('/usr/lib', '/usr/lib64', '/usr/share/fonts',
                          '/usr/share/fontconfig', '/etc/fonts'))
_SHA = re.compile(r'[0-9a-f]{64}\Z')


def _file_sha(parent, name, *, executable=False, destination=None):
    fd = ui_fs.open_file(parent, name, os.O_RDONLY)
    try:
        before = os.fstat(fd)
        if (before.st_size > 256 * 1024 * 1024 or before.st_mode & 0o6000
                or (executable and not before.st_mode & 0o111)):
            raise ValueError('unsupported runtime file')
        hashed, total = hashlib.sha256(), 0
        while data := os.read(fd, 1024 * 1024):
            total += len(data)
            if total > 256 * 1024 * 1024:
                raise ValueError('runtime file limit')
            hashed.update(data)
            if destination is not None:
                offset = 0
                while offset < len(data):
                    written = os.write(destination, data[offset:])
                    if written <= 0:
                        raise ValueError('runtime snapshot write failed')
                    offset += written
        after = os.fstat(fd)
        keys = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if total != before.st_size or any(getattr(before, key) != getattr(after, key) for key in keys):
            raise ValueError('runtime file changed')
        return hashed.hexdigest(), total
    finally:
        os.close(fd)


def _absolute_file_sha(path):
    path = Path(path)
    parent = ui_fs.open_absolute_dir(path.parent)
    try:
        return _file_sha(parent, path.name, executable=True)[0]
    finally:
        ui_fs.close_dir(parent)


def _browser_inventory(root):
    handle = ui_fs.open_absolute_dir(Path(root))
    result, total = {}, 0
    def walk(directory, prefix='', depth=0):
        nonlocal total
        if depth > 4:
            raise ValueError('runtime depth limit')
        names = ui_fs.listdir(directory)
        if len(names) + len(result) > 512:
            raise ValueError('runtime file count limit')
        for name in sorted(names):
            mode = ui_fs.stat_at(directory, name).st_mode
            path = prefix + name
            if stat.S_ISDIR(mode):
                child = ui_fs.open_child_dir(directory, name)
                try:
                    walk(child, path + '/', depth + 1)
                finally:
                    ui_fs.close_dir(child)
            elif stat.S_ISREG(mode):
                sha, size = _file_sha(directory, name, executable=path == 'chrome-headless-shell')
                total += size
                if len(result) >= 512 or total > 512 * 1024 * 1024:
                    raise ValueError('runtime inventory limit')
                result[path] = sha
            else:
                raise ValueError('runtime links and special files refused')
    try:
        walk(handle)
        return result
    finally:
        ui_fs.close_dir(handle)


class _NativeProcess:
    """Own one private process group and pipes, not an OS isolation claim.

    LinuxRenderProvider adds namespaces, read-only mounts and inherited kernel
    limits. The watchdog additionally stops the group on aggregate observed
    RSS >512MiB or >64 descendants; this polling guard is NOT a cgroup cap.
    The provider never treats this helper alone as an isolated renderer.
    """
    def __init__(self, argv, *, timeout, max_bytes, max_events,
                 max_rss_bytes=512 * 1024 * 1024, max_processes=64, passed_fds=()):
        self.argv = argv
        self.timeout, self.max_bytes, self.max_events = timeout, max_bytes, max_events
        self.max_rss_bytes, self.max_processes = max_rss_bytes, max_processes
        self.passed_fds = passed_fds
        self.pid = None
        self.channel = None
        self.reaped = False
        self.reason = None
        self._tracked = {}
        self._stop = threading.Event()
        self._thread = None
        self._stderr = None
        self._stderr_bytes = 0

    @staticmethod
    def _stat(pid):
        try:
            with open('/proc/%d/stat' % pid, 'rb') as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError('oversized process metadata')
            fields = raw.rsplit(b')', 1)[1].split()
            return fields[19], int(fields[21]) * os.sysconf('SC_PAGE_SIZE'), int(fields[1])
        except FileNotFoundError:
            return None

    def _track(self):
        pending, seen, rss, threads_seen = [(self.pid, None)], set(), 0, 0
        while pending:
            pid, parent = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if len(seen) > self.max_processes:
                raise CdpError('RENDER_PROCESS_LIMIT')
            current = self._stat(pid)
            if current is None or (parent is not None and current[2] != parent):
                continue
            self._tracked[pid] = current[0]
            rss += current[1]
            if rss > self.max_rss_bytes:
                raise CdpError('RENDER_MEMORY_LIMIT')
            try:
                # children is per-thread, not per thread group. Browser worker
                # threads can fork, so the leader's file is insufficient.
                with os.scandir('/proc/%d/task' % pid) as tasks:
                    for task in tasks:
                        threads_seen += 1
                        if threads_seen > 1024:
                            raise CdpError('RENDER_PROCESS_LIMIT')
                        try:
                            with open('/proc/%d/task/%s/children' % (pid, task.name), 'rb') as stream:
                                children = stream.read(4097)
                        except FileNotFoundError:
                            continue
                        if len(children) > 4096:
                            raise CdpError('RENDER_PROCESS_LIMIT')
                        pending.extend((int(value), pid) for value in children.split())
            except FileNotFoundError:
                pass

    def remaining(self):
        return tuple(pid for pid, start in self._tracked.items()
                     if (current := self._stat(pid)) is not None and current[0] == start)

    def _kill(self):
        # The leader is not reaped until this owner finishes, so its PID cannot
        # be reused for a different process group during this operation.
        if self.pid is not None and not self.reaped:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _watch(self):
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(self._stderr, selectors.EVENT_READ)
                while not self._stop.is_set():
                    self._track()
                    if time.monotonic() >= self._deadline:
                        raise CdpError('RENDER_TIMEOUT')
                    for _key, _mask in selector.select(min(0.02, max(0, self._deadline - time.monotonic()))):
                        try:
                            chunk = os.read(self._stderr, 65536)
                        except BlockingIOError:
                            continue
                        if chunk:
                            self._stderr_bytes += len(chunk)
                        else:
                            selector.unregister(self._stderr)
                    received = self.channel.received_bytes if self.channel is not None else 0
                    if self._stderr_bytes + received > self.max_bytes:
                        raise CdpError('RENDER_OUTPUT_LIMIT')
        except Exception as exc:
            self.reason = exc.reason_code if isinstance(exc, CdpError) else 'RENDER_SUPERVISION_FAILED'
            self._kill()

    def __enter__(self):
        if (sys.platform != 'linux' or not hasattr(os, 'POSIX_SPAWN_CLOSEFROM')
                or type(self.argv) is not tuple or not self.argv
                or any(type(arg) is not str or '\0' in arg for arg in self.argv)
                or type(self.timeout) not in (int, float) or not 0 < self.timeout <= 35
                or type(self.max_bytes) is not int or not 64 <= self.max_bytes <= 2_000_000
                or type(self.max_events) is not int or not 0 <= self.max_events <= 2000
                or type(self.max_rss_bytes) is not int or not 65536 <= self.max_rss_bytes <= 512 * 1024 * 1024
                or type(self.max_processes) is not int or not 1 <= self.max_processes <= 64):
            raise CdpError('RENDER_PROCESS_UNAVAILABLE')
        if (type(self.passed_fds) is not tuple or len(self.passed_fds) > 520
                or any(type(fd) is not int or fd < 0 for fd in self.passed_fds)):
            raise CdpError('RENDER_PROCESS_UNAVAILABLE')
        # Duplicate above fd4 before constructing spawn actions, avoiding the
        # classic fd3/fd4 alias/clobber when the controller has few open files.
        import fcntl
        descriptors = []
        source_floor = 16 + len(self.passed_fds)
        def pipe():
            pair = os.pipe()
            try:
                result = []
                for fd in pair:
                    duplicate = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, source_floor)
                    descriptors.append(duplicate)
                    result.append(duplicate)
                return result
            finally:
                for fd in pair:
                    os.close(fd)
        try:
            browser_read, controller_write = pipe()
            controller_read, browser_write = pipe()
            stderr_read, stderr_write = pipe()
            original_null = os.open('/dev/null', os.O_RDWR)
            try:
                null = fcntl.fcntl(original_null, fcntl.F_DUPFD_CLOEXEC, source_floor)
            finally:
                os.close(original_null)
            descriptors.append(null)
            actions = [(os.POSIX_SPAWN_DUP2, browser_read, 3), (os.POSIX_SPAWN_DUP2, browser_write, 4),
                (os.POSIX_SPAWN_DUP2, null, 0), (os.POSIX_SPAWN_DUP2, null, 1),
                (os.POSIX_SPAWN_DUP2, stderr_write, 2)]
            for index, fd in enumerate(self.passed_fds):
                duplicate = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, source_floor)
                descriptors.append(duplicate)
                actions.append((os.POSIX_SPAWN_DUP2, duplicate, 5 + index))
            actions.append((os.POSIX_SPAWN_CLOSEFROM, 5 + len(self.passed_fds)))
            self._deadline = time.monotonic() + self.timeout
            self.pid = os.posix_spawn(self.argv[0], self.argv, {'LANG': 'C.UTF-8', 'PATH': '/usr/bin'},
                                      file_actions=actions, setsid=True)
            for fd in (browser_read, browser_write, stderr_write, null):
                os.close(fd)
                descriptors.remove(fd)
            self._stderr = stderr_read
            os.set_blocking(stderr_read, False)
            # CdpPipe owns these even if its constructor fails.
            descriptors.remove(controller_read)
            descriptors.remove(controller_write)
            self.channel = CdpPipe(controller_read, controller_write,
                timeout_seconds=max(0.001, self._deadline - time.monotonic()),
                max_bytes=self.max_bytes, max_events=self.max_events)
            descriptors.remove(stderr_read)
            self._track()
            self._thread = threading.Thread(target=self._watch, name='aek-render-owner', daemon=True)
            self._thread.start()
            return self.channel
        except BaseException:
            self._kill()
            if self.pid is not None:
                os.waitpid(self.pid, 0)
                self.reaped = True
            if self.channel is not None:
                self.channel.close()
            if self._stderr is not None and self._stderr not in descriptors:
                os.close(self._stderr)
            self._stderr = None
            raise
        finally:
            for fd in descriptors:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def __exit__(self, *_exception):
        try:
            if (self.reason is None and len(_exception) >= 2
                    and isinstance(_exception[1], CdpError)
                    and _exception[1].reason_code == 'CDP_TIMEOUT'):
                self.reason = 'RENDER_TIMEOUT'
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=1)
                if self._thread.is_alive():
                    self.reason = 'RENDER_CLEANUP_INCOMPLETE'
            if self.channel is not None:
                self.channel.close()
            try:
                self._track()
            except Exception as exc:
                self.reason = self.reason or (exc.reason_code if isinstance(exc, CdpError)
                                             else 'RENDER_SUPERVISION_FAILED')
            self._kill()
            if self.pid is not None and not self.reaped:
                until = time.monotonic() + 2
                while not self.reaped and time.monotonic() < until:
                    waited, _status = os.waitpid(self.pid, os.WNOHANG)
                    self.reaped = waited == self.pid
                    if not self.reaped:
                        time.sleep(0.01)
                while self.reaped and self.remaining() and time.monotonic() < until:
                    time.sleep(0.01)
                if not self.reaped or self.remaining():
                    self.reason = 'RENDER_CLEANUP_INCOMPLETE'
        finally:
            if self._stderr is not None:
                # Recheck bytes which arrived after the last watchdog poll.
                # Only the count is retained, never raw browser diagnostics.
                try:
                    while self._stderr_bytes <= self.max_bytes:
                        chunk = os.read(self._stderr, min(65536, self.max_bytes - self._stderr_bytes + 1))
                        if not chunk:
                            break
                        self._stderr_bytes += len(chunk)
                except BlockingIOError:
                    pass
                if self._stderr_bytes + self.channel.received_bytes > self.max_bytes:
                    self.reason = 'RENDER_OUTPUT_LIMIT'
                os.close(self._stderr)
                self._stderr = None
        if self.reason:
            raise CdpError(self.reason)


@dataclass(frozen=True)
class LinuxRuntime:
    browser_root: str
    bubblewrap: str
    prlimit: str
    browser_files: tuple[tuple[str, str], ...]
    bubblewrap_sha256: str
    prlimit_sha256: str
    readonly_roots: tuple[str, ...]
    browser_product: str = 'HeadlessChrome/155.0.8059.79'

    @property
    def sha256(self):
        return digest(asdict(self))


@dataclass(frozen=True)
class LinuxRenderPlan:
    repository_ref: str
    scope_ref: str
    identity_digest: str
    runtime_sha256: str
    input_sha256: str
    conditions_json: str
    timeout_seconds: float = 35
    max_output_bytes: int = 2_000_000
    max_items: int = 2000

    @property
    def plan_digest(self):
        return digest(asdict(self))

    def binding(self):
        return runtime_binding(plan_digest=self.plan_digest, repository_ref=self.repository_ref,
            scope_ref=self.scope_ref, identity_digest=self.identity_digest,
            tool_id='aek-static-render-linux-v2', executable_digest=self.runtime_sha256,
            action='render-static', network_mode='offline', write_scope=(),
            timeout_seconds=self.timeout_seconds, max_output_bytes=self.max_output_bytes,
            max_items=self.max_items)


def render_plan(runtime, input_sha256, conditions, *, repository_ref, scope_ref,
                identity_digest, timeout_seconds=35, max_output_bytes=2_000_000):
    """Prepare data only. The original host must approve its exact binding."""
    from aek.core.security.authority import canonical
    if type(runtime) is not LinuxRuntime or type(conditions) is not RenderConditions:
        raise ValueError('invalid native render plan')
    plan = LinuxRenderPlan(repository_ref, scope_ref, identity_digest, runtime.sha256,
        input_sha256, canonical(conditions.checked()).decode('utf-8'), timeout_seconds, max_output_bytes)
    plan.binding()
    if (type(input_sha256) is not str or not _SHA.fullmatch(input_sha256)
            or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 35
            or type(max_output_bytes) is not int or not 8192 <= max_output_bytes <= 2_000_000
            or any(type(value) is not str or not value or '\0' in value
                   for value in (repository_ref, scope_ref, identity_digest))):
        raise ValueError('invalid native render budget or identity')
    return plan


def linux_runtime_fingerprint(runtime):
    """Measure the original runtime bytes/roots, without adoption or execution.

    The descriptor SHA is returned only after every existing material check;
    this function is suitable for the original adoption bridge without calling
    back into the provider and recursively re-entering adoption.
    """
    try:
        if (type(runtime) is not LinuxRuntime
                or runtime.browser_product != 'HeadlessChrome/155.0.8059.79'
                or type(runtime.readonly_roots) is not tuple
                or frozenset(runtime.readonly_roots) != _SYSTEM_ROOTS
                or len(runtime.readonly_roots) != len(_SYSTEM_ROOTS)
                or type(runtime.browser_files) is not tuple or not 1 <= len(runtime.browser_files) <= 512
                or any(type(item) is not tuple or len(item) != 2 or type(item[0]) is not str
                       or type(item[1]) is not str or not _SHA.fullmatch(item[1])
                       for item in runtime.browser_files)
                or len(dict(runtime.browser_files)) != len(runtime.browser_files)
                or 'chrome-headless-shell' not in dict(runtime.browser_files)
                or _browser_inventory(runtime.browser_root) != dict(runtime.browser_files)
                or _absolute_file_sha(runtime.bubblewrap) != runtime.bubblewrap_sha256
                or _absolute_file_sha(runtime.prlimit) != runtime.prlimit_sha256):
            return None
        for path in runtime.readonly_roots:
            directory = ui_fs.open_absolute_dir(Path(path))
            ui_fs.close_dir(directory)
        return runtime.sha256
    except (OSError, TypeError, ValueError):
        return None


class LinuxRenderProvider:
    def __init__(self, runtime, *, authority, grant, adoption_validator):
        self.runtime, self.authority, self.grant = runtime, authority, grant
        self._adoption_validator = adoption_validator
        self._execution_lock = threading.Lock()
        self._session = None
        self._phase = 'IDLE'

    def _runtime_current(self):
        try:
            if linux_runtime_fingerprint(self.runtime) is None:
                return False
            return self._adoption_validator(self.runtime) is True
        except (OSError, TypeError, ValueError):
            return False

    def _authorize(self, snapshot_sha256, conditions, plan):
        from aek.core.security.authority import canonical
        if (sys.platform != 'linux' or type(self.runtime) is not LinuxRuntime
                or type(plan) is not LinuxRenderPlan or type(self.grant) is not RuntimeGrant
                or self.authority is None or not callable(self._adoption_validator)
                or type(plan.timeout_seconds) not in (int, float) or not 0 < plan.timeout_seconds <= 35
                or type(plan.max_output_bytes) is not int or not 8192 <= plan.max_output_bytes <= 2_000_000
                or type(plan.max_items) is not int or not 1 <= plan.max_items <= 2000
                or plan.runtime_sha256 != self.runtime.sha256
                or plan.input_sha256 != snapshot_sha256
                or plan.conditions_json != canonical(conditions).decode('utf-8')):
            raise ProviderUnavailable('approved native runtime unavailable')
        binding = plan.binding()
        self.authority.validate_runtime_grant(self.grant,
            **{key: value for key, value in binding.items() if key != 'operation'})
        if self._adoption_validator(self.runtime) is not True:
            raise ProviderUnavailable('runtime adoption is no longer current')

    @contextmanager
    def _pin_runtime(self):
        """Immutable verified byte copies and pinned system directory handles.

        Names are not reused for exec/mount after validation. Seals prevent
        modifications through an accidentally retained writable memfd as well.
        """
        import fcntl
        owned, browser, system = [], [], []
        total = 0
        def snapshot(parent, name, expected, *, executable):
            nonlocal total
            fd = os.memfd_create('aek-render-runtime', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            owned.append(fd)
            os.fchmod(fd, 0o755 if executable else 0o644)
            sha, size = _file_sha(parent, name, executable=executable, destination=fd)
            total += size
            if sha != expected or total > 512 * 1024 * 1024:
                raise ValueError('runtime snapshot is not the adopted content')
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS,
                fcntl.F_SEAL_WRITE | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SEAL)
            os.lseek(fd, 0, os.SEEK_SET)
            return 4 + len(owned)
        def absolute(path, sha):
            path = Path(path)
            parent = ui_fs.open_absolute_dir(path.parent)
            try:
                return snapshot(parent, path.name, sha, executable=True)
            finally:
                ui_fs.close_dir(parent)
        try:
            bwrap = absolute(self.runtime.bubblewrap, self.runtime.bubblewrap_sha256)
            prlimit = absolute(self.runtime.prlimit, self.runtime.prlimit_sha256)
            for name, sha in self.runtime.browser_files:
                components = name.split('/')
                parent = ui_fs.open_absolute_dir(Path(self.runtime.browser_root))
                try:
                    for component in components[:-1]:
                        child = ui_fs.open_child_dir(parent, component)
                        ui_fs.close_dir(parent)
                        parent = child
                    fd = snapshot(parent, components[-1], sha, executable=name == 'chrome-headless-shell')
                    browser.append((name, fd))
                finally:
                    ui_fs.close_dir(parent)
            for root in self.runtime.readonly_roots:
                owned.append(ui_fs.open_absolute_dir(Path(root)))
                system.append((root, 4 + len(owned)))
            yield tuple(owned), bwrap, prlimit, tuple(browser), tuple(system)
        finally:
            for fd in owned:
                os.close(fd)

    def _argv(self, plan, pinned):
        runtime = self.runtime
        _fds, bwrap, prlimit, browser, system = pinned
        result = ['/proc/self/fd/' + str(bwrap), '--unshare-user', '--unshare-net', '--unshare-pid',
            '--unshare-ipc', '--unshare-uts', '--die-with-parent', '--clearenv']
        for root, fd in system:
            result.extend(('--ro-bind-fd', str(fd), root))
        # Keep copied files linked inside private bounded mounts. bind-fd
        # cannot realpath a memfd; bind-data unlinks its backing file, which
        # breaks Chromium's /proc/self/exe-based zygote path. The completed
        # private mounts are remounted read-only before any browser executes.
        result.extend(('--size', '536870912', '--tmpfs', '/runtime',
                       '--size', '1048576', '--tmpfs', '/tools', '--perms', '0755',
                       '--file', str(prlimit), '/tools/prlimit', '--remount-ro', '/tools'))
        directories = set()
        for name, fd in browser:
            parts = name.split('/')
            for index in range(1, len(parts)):
                directory = '/runtime/' + '/'.join(parts[:index])
                if directory not in directories:
                    directories.add(directory)
                    result.extend(('--dir', directory))
            result.extend(('--perms', '0755' if name == 'chrome-headless-shell' else '0644',
                           '--file', str(fd), '/runtime/' + name))
        result.extend(('--remount-ro', '/runtime'))
        result.extend(('--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
            '--proc', '/proc', '--dev', '/dev', '--size', '134217728', '--tmpfs', '/tmp',
            '--dir', '/home/aek', '--setenv', 'HOME', '/home/aek',
            '--setenv', 'LANG', 'C.UTF-8', '--chdir', '/tmp', '--', '/tools/prlimit',
            '--cpu=25', '--core=0', '--fsize=8388608', '--nofile=256',
            '--', '/runtime/chrome-headless-shell', '--headless', '--remote-debugging-pipe',
            '--browser-subprocess-path=/runtime/chrome-headless-shell',
            '--disable-background-networking', '--disable-component-update', '--disable-sync',
            '--no-first-run', '--no-default-browser-check', '--user-data-dir=/tmp/profile', 'about:blank'))
        return tuple(result)

    @contextmanager
    def open(self, snapshot, conditions, plan):
        try:
            self._authorize(hashlib.sha256(snapshot).hexdigest(), conditions, plan)
        except (TypeError, ValueError):
            raise ProviderUnavailable('approved native runtime unavailable') from None
        if not self._runtime_current():
            raise ProviderUnavailable('runtime content is no longer current')
        if not self._execution_lock.acquire(blocking=False):
            raise ProviderUnavailable('another render is still active')
        try:
            self._session, self._phase = None, 'STARTING'
            with self._pin_runtime() as pinned, _NativeProcess(self._argv(plan, pinned),
                    timeout=plan.timeout_seconds, max_bytes=plan.max_output_bytes,
                    max_events=plan.max_items, passed_fds=pinned[0]) as channel:
                if channel.rpc('Browser.getVersion').get('product') != self.runtime.browser_product:
                    raise CdpError('RENDER_RUNTIME_MISMATCH')
                # Do not yield a process until both the pinned plan and the
                # actual runtime bytes still satisfy the original authority.
                self._authorize(hashlib.sha256(snapshot).hexdigest(), conditions, plan)
                if not self._runtime_current():
                    raise CdpError('RENDER_RUNTIME_MISMATCH')
                session = RenderSession(channel, self.runtime.sha256, plan.plan_digest,
                                        self.runtime.browser_product)
                self._session, self._phase = session, 'ACTIVE'
                yield session
            self._phase = 'CLEANED'
        except BaseException:
            self._phase = 'FAILED'
            raise
        finally:
            self._execution_lock.release()

    def validate_current(self, session, snapshot_sha256, conditions, plan):
        self._authorize(snapshot_sha256, conditions, plan)
        return (self._session is session and self._phase in ('ACTIVE', 'CLEANED')
            and session.runtime_sha256 == self.runtime.sha256 and session.plan_sha256 == plan.plan_digest
            and self._runtime_current())
