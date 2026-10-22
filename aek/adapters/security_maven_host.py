"""Fixed Linux offline Maven profile; original host owns approval and source.

Only controller manifests are staged, never a user cache or original source
mount. Private staging is not sealed against arbitrary same-UID controller
code; that code is outside the original host's threat model.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile

from aek.adapters.security_maven import prepare_maven_resolution
from aek.adapters.security_process import ProcessPort
from aek.adapters.security_runtime import BoundRuntimeAdoption
from aek.application.local_host import LocalToolCompletion, PreparedLocalTool
from aek.core.context.identity import identity_as_dict
from aek.core.security.authority import SecurityAuthorityError, canonical, digest, runtime_binding

PROFILE = 'maven-offline-v1'
MAX_STAGE_BYTES = 2 * 1024 ** 3
PLUGIN_JAR = 'cache/org/apache/maven/plugins/maven-dependency-plugin/3.11.0/maven-dependency-plugin-3.11.0.jar'


def _relative(value):
    if (type(value) is not str or not value or len(value) > 2048 or '\\' in value
            or ':' in value or any(ord(c) < 32 for c in value)
            or PurePosixPath(value).is_absolute() or any(p in {'', '.', '..'} for p in value.split('/'))):
        raise SecurityAuthorityError('MAVEN_MANIFEST_INVALID')
    return value


def _open(path, *, directory=False):
    """Walk absolute controller paths without following any source symlink."""
    if type(path) is not str or not path.startswith('/'):
        raise SecurityAuthorityError('MAVEN_MANIFEST_INVALID')
    parts = path[1:].split('/')
    if any(p in {'', '.', '..'} for p in parts):
        raise SecurityAuthorityError('MAVEN_MANIFEST_INVALID')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(parts) - 1 or directory:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _measure(row, destination=None, *, limit=512 * 1024 ** 2):
    path, mount, expected, executable = row
    if (type(row) is not tuple or len(row) != 4 or type(executable) is not bool
            or type(expected) is not str or re.fullmatch('[a-f0-9]{64}', expected) is None):
        raise SecurityAuthorityError('MAVEN_MANIFEST_INVALID')
    _relative(mount)
    fd = _open(path)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= limit
                or executable and not before.st_mode & 0o111):
            raise SecurityAuthorityError('MAVEN_INPUT_REJECTED')
        value, size = hashlib.sha256(), 0
        while chunk := os.read(fd, 65536):
            size += len(chunk)
            if size > limit:
                raise SecurityAuthorityError('MAVEN_INPUT_LIMIT')
            value.update(chunk)
            if destination is not None:
                destination.write(chunk)
        after = os.fstat(fd)
        if (_identity(before) != _identity(after) or size != before.st_size or value.hexdigest() != expected):
            raise SecurityAuthorityError('MAVEN_INPUT_CHANGED')
        return size
    finally:
        os.close(fd)


def _identity(info):
    # Reading may legitimately update atime; content and replacement fences do
    # require stable nanosecond mtime/ctime, ownership, mode, inode and size.
    return tuple(getattr(info, name) for name in ('st_dev', 'st_ino', 'st_mode', 'st_nlink',
        'st_uid', 'st_gid', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))


def _rows(rows, prefixes):
    if type(rows) is not tuple or not 1 <= len(rows) <= 4096:
        raise SecurityAuthorityError('MAVEN_MANIFEST_INVALID')
    mounts = set()
    for row in rows:
        if type(row) is not tuple or len(row) != 4:
            raise SecurityAuthorityError('MAVEN_MANIFEST_INVALID')
        mount = _relative(row[1])
        if mount.split('/')[0] not in prefixes or mount in mounts:
            raise SecurityAuthorityError('MAVEN_MANIFEST_INVALID')
        mounts.add(mount)
    if any('/'.join(m.split('/')[:i]) in mounts for m in mounts for i in range(1, len(m.split('/')))):
        raise SecurityAuthorityError('MAVEN_MANIFEST_INVALID')
    return mounts


@dataclass(frozen=True)
class MavenRuntime:
    bubblewrap: str
    bubblewrap_sha256: str
    prlimit: str
    prlimit_sha256: str
    files: tuple


@dataclass(frozen=True)
class MavenTarget:
    name: str
    module: str
    pom: str
    profiles: tuple
    files: tuple
    models: tuple


def maven_runtime_fingerprint(runtime):
    """Measure all configured binary/JDK/Maven/cache bytes, not just a label."""
    if type(runtime) is not MavenRuntime:
        raise SecurityAuthorityError('MAVEN_PROVIDER_UNAVAILABLE')
    mounts = _rows(runtime.files, {'jdk', 'maven', 'cache'})
    if not {'jdk/bin/java', 'maven/bin/m2.conf', PLUGIN_JAR} <= mounts or not any(p.startswith('maven/boot/') for p in mounts):
        raise SecurityAuthorityError('MAVEN_MANIFEST_INVALID')
    total = 0
    for row in ((runtime.bubblewrap, 'bubblewrap', runtime.bubblewrap_sha256, True),
                (runtime.prlimit, 'prlimit', runtime.prlimit_sha256, True), *runtime.files):
        total += _measure(row)
        if total > MAX_STAGE_BYTES:
            raise SecurityAuthorityError('MAVEN_INPUT_LIMIT')
    return digest(asdict(runtime))


def _sealed(row):
    import fcntl
    fd = os.memfd_create('aek-maven-runtime', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        with os.fdopen(os.dup(fd), 'wb') as output:
            _measure(row, output, limit=32 * 1024 ** 2)
        os.fchmod(fd, 0o500)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise


class MavenLocalProvider:
    def __init__(self, runtime=None, *, authority=None, adoption=None, targets=(), required_components=()):
        self.runtime, self.authority, self.adoption = runtime, authority, adoption
        self.targets, self.required_components = targets, required_components
        self._bound = self._snapshot()

    def _snapshot(self):
        return (self.runtime, id(self.authority), id(self.adoption), self.targets, self.required_components)

    def prepare(self, data):
        if (sys.platform != 'linux' or self.runtime is None or self.authority is None
                or self._snapshot() != self._bound or type(self.adoption) is not BoundRuntimeAdoption
                or self.adoption._authority is not self.authority
                or self.adoption._fingerprint is not maven_runtime_fingerprint
                or self.adoption._required_components != self.required_components
                or self.adoption(self.runtime) is not True):
            raise SecurityAuthorityError('MAVEN_PROVIDER_UNAVAILABLE')
        if (type(data) is not dict or set(data) != {'profile_id', 'targets'} or data['profile_id'] != PROFILE
                or type(data['targets']) is not list or len(data['targets']) != 1
                or type(self.targets) is not tuple or any(type(t) is not MavenTarget for t in self.targets)):
            raise SecurityAuthorityError('MAVEN_TARGET_REJECTED')
        selected = [t for t in self.targets if t.name == data['targets'][0]]
        if len(selected) != 1:
            raise SecurityAuthorityError('MAVEN_TARGET_REJECTED')
        target = selected[0]
        _relative(target.module) if target.module != '.' else None
        mounts = _rows(target.files, {'model'})
        if (target.pom not in mounts or type(target.profiles) is not tuple or len(target.profiles) > 32
                or len(set(target.profiles)) != len(target.profiles)
                or any(type(p) is not str or re.fullmatch('[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}', p) is None for p in target.profiles)
                or type(target.models) is not tuple or not 1 <= len(target.models) <= 128):
            raise SecurityAuthorityError('MAVEN_MANIFEST_INVALID')
        paths, settings, model_total = set(), [], 0
        for logical, role, mount in target.models:
            _relative(logical)
            if (logical in paths or mount not in mounts or role not in {'module', 'parent', 'bom', 'settings', 'profile'}):
                raise SecurityAuthorityError('MAVEN_MANIFEST_INVALID')
            paths.add(logical)
            if role == 'settings':
                settings.append(mount)
        if len(settings) != 1 or not any(role == 'module' and mount == target.pom for _, role, mount in target.models):
            raise SecurityAuthorityError('MAVEN_MANIFEST_INVALID')
        for row in target.files:
            model_total += _measure(row, limit=2_000_000)
        if model_total > 2_000_000:
            raise SecurityAuthorityError('MAVEN_INPUT_LIMIT')
        fingerprint = maven_runtime_fingerprint(self.runtime)
        argv = ['/exec/prlimit', '--cpu=120', '--as=2147483648', '--fsize=2000000', '--nofile=128', '--',
            '/input/jdk/bin/java', '-Xmx512m', '-Djava.io.tmpdir=/tmp', '-Duser.home=/home/aek',
            '-XX:MaxMetaspaceSize=128m', '-XX:CompressedClassSpaceSize=64m', '-XX:ReservedCodeCacheSize=64m',
            '-Dmaven.home=/input/maven', '-Dmaven.multiModuleProjectDirectory=/input/model',
            '-Dclassworlds.conf=/input/maven/bin/m2.conf', '-classpath', '/input/maven/boot/*',
            'org.codehaus.plexus.classworlds.launcher.Launcher', '-B', '-ntp', '-N', '-o',
            '-s', '/input/' + settings[0], '-gs', '/input/' + settings[0],
            '-Dmaven.repo.local=/input/cache', '-f', '/input/' + target.pom]
        if target.profiles:
            argv.append('-P' + ','.join(target.profiles))
        argv += ['org.apache.maven.plugins:maven-dependency-plugin:3.11.0:tree', '-DoutputType=json',
                 '-DoutputEncoding=UTF-8', '-DoutputFile=/out/tree.json']
        plan = {'tool_id': PROFILE, 'runtime_sha256': fingerprint, 'target': asdict(target), 'argv': argv,
            'read_scope': ['fixed-manifest', 'system-libraries', 'fixed-system-devices'],
            'write_scope': ['private-output', 'private-tmp', 'private-home'],
            'network_mode': 'offline', 'boundary': 'linux-bwrap-private-root', 'timeout_seconds': 180,
            'max_output_bytes': 2_000_000, 'max_items': 128, 'tmpfs_bytes': [134217728, 134217728, 16777216, 1048576]}
        plan = json.loads(canonical(plan))  # Same primitive shape after authenticated journal recovery.
        binding = runtime_binding(plan_digest=digest(plan), repository_ref=self.authority.repository_ref,
            scope_ref=self.authority.scope_ref, identity_digest=self.authority.current_identity.envelope_digest,
            tool_id=PROFILE, executable_digest=fingerprint, action='resolve', network_mode='offline',
            write_scope=tuple(plan['write_scope']), timeout_seconds=180, max_output_bytes=2_000_000, max_items=128)
        return PreparedLocalTool(plan, binding, 'maven-' + digest(binding), target)

    def revalidate(self, prepared):
        try:
            current = self.prepare({'profile_id': PROFILE, 'targets': [prepared.provider_data.name]})
            return (current.provider_data == prepared.provider_data and current.source_ref == prepared.source_ref
                    and canonical(current.plan) == canonical(prepared.plan)
                    and canonical(current.binding) == canonical(prepared.binding) and self._snapshot() == self._bound)
        except Exception:
            return False

    def execute(self, prepared, grant, authority):
        if authority is not self.authority or self.revalidate(prepared) is not True:
            raise SecurityAuthorityError('HOST_PLAN_STALE')
        binding = {k: v for k, v in prepared.binding.items() if k != 'operation'}
        authority.validate_runtime_grant(grant, **binding)
        target, runtime = prepared.provider_data, self.runtime
        with tempfile.TemporaryDirectory(prefix='aek-maven-') as directory:
            tree, out = Path(directory) / 'input', Path(directory) / 'out'
            tree.mkdir(mode=0o700)
            out.mkdir(mode=0o700)
            (out / 'tree.json').touch(mode=0o600)
            total, model_inputs, fds = 0, {}, []
            try:
                for row in (*runtime.files, *target.files):
                    destination = tree / row[1]
                    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with destination.open('xb') as output:
                        total += _measure(row, output)
                    destination.chmod(0o500 if row[3] else 0o400)
                    if total > MAX_STAGE_BYTES:
                        raise SecurityAuthorityError('MAVEN_INPUT_LIMIT')
                for logical, _role, mount in target.models:
                    model_inputs[logical] = (tree / mount).read_bytes()
                for path, _, _ in os.walk(tree, topdown=False):
                    os.chmod(path, 0o500)
                for row in ((runtime.bubblewrap, 'bubblewrap', runtime.bubblewrap_sha256, True),
                            (runtime.prlimit, 'prlimit', runtime.prlimit_sha256, True)):
                    fds.append(_sealed(row))
                fds.append(_open(str(tree), directory=True))
                fds.append(_open(str(out / 'tree.json')))
                for path in ('/usr/lib', '/usr/lib64'):
                    fds.append(_open(path, directory=True))
                for path, minor in (('/dev/null', 3), ('/dev/urandom', 9)):
                    fds.append(_open(path))
                    info = os.fstat(fds[-1])
                    if not stat.S_ISCHR(info.st_mode) or info.st_rdev != os.makedev(1, minor):
                        raise SecurityAuthorityError('MAVEN_PLATFORM_UNAVAILABLE')
                wrap, limit, input_fd, out_fd, lib, lib64, null, random = [f'/proc/self/fd/{fd}' for fd in fds]
                argv = (wrap, '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
                    '--clearenv', '--size', '134217728', '--tmpfs', '/', '--proc', '/proc', '--dir', '/dev',
                    '--dev-bind', null, '/dev/null', '--dev-bind', random, '/dev/urandom', '--symlink', 'urandom', '/dev/random',
                    '--size', '134217728', '--tmpfs', '/tmp', '--size', '16777216', '--tmpfs', '/out',
                    '--dir', '/home/aek', '--dir', '/usr',
                    '--ro-bind', lib, '/usr/lib', '--ro-bind', lib64, '/usr/lib64',
                    '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
                    '--ro-bind', input_fd, '/input', '--bind', out_fd, '/out/tree.json',
                    # bwrap cannot realpath an anonymous memfd for ro-bind.
                    '--size', '1048576', '--tmpfs', '/exec', '--perms', '0500',
                    '--file', str(fds[1]), '/exec/prlimit', '--remount-ro', '/exec', '--setenv', 'HOME', '/home/aek',
                    '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'LC_ALL', 'C.UTF-8',
                    '--chdir', '/input/model', '--', *prepared.plan['argv'])
                if self.revalidate(prepared) is not True:
                    raise SecurityAuthorityError('HOST_PLAN_STALE')
                authority.validate_runtime_grant(grant, **binding)
                result = ProcessPort().run(argv, out, timeout_seconds=180, max_output_bytes=2_000_000,
                    output_file=out / 'tree.json', environment={'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}, pass_fds=tuple(fds))
                if (result.exit_code != 0 or result.reason_code != 'PROCESS_COMPLETED'
                        or result.cleanup_complete is not True):
                    raise SecurityAuthorityError('MAVEN_EXECUTION_INCOMPLETE')
                output_fd = _open(str(out / 'tree.json'))
                try:
                    info = os.fstat(output_fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
                        raise SecurityAuthorityError('MAVEN_OUTPUT_REJECTED')
                    raw = os.read(output_fd, 2_000_001)
                    if _identity(os.fstat(output_fd)) != _identity(info) or len(raw) != info.st_size:
                        raise SecurityAuthorityError('MAVEN_OUTPUT_REJECTED')
                finally:
                    os.close(output_fd)
            finally:
                for fd in reversed(fds):
                    os.close(fd)
                # Restore owner write permission solely for private-tree cleanup.
                for path, _, _ in os.walk(tree):
                    os.chmod(path, 0o700)
        if self.revalidate(prepared) is not True:
            raise SecurityAuthorityError('HOST_PLAN_STALE')
        authority.validate_runtime_grant(grant, **binding)
        metadata = {'id': prepared.source_ref, 'ref': prepared.source_ref, 'run_id': None, 'producer_ref': PROFILE,
            'identity': identity_as_dict(authority.current_identity), 'required_components': list(self.required_components),
            'kind': 'inventory', 'status': 'DECLARED', 'checked_at': authority._clock().isoformat(),
            'scope_ref': authority.scope_ref, 'tool_version': '3.11.0', 'rules_digest': prepared.binding['plan_digest'],
            'database_digest': None, 'coverage': 'PARTIAL', 'coverage_gaps': [],
            'summary': 'Offline Maven resolution; model/profile completeness and scanner mapping unestablished'}
        sidecar = {'schema_version': 2, 'producer': {'coordinate': 'org.apache.maven.plugins:maven-dependency-plugin',
            'version': '3.11.0', 'format': 'tree-json', 'tool_sha256': next(row[2] for row in runtime.files if row[1] == PLUGIN_JAR)},
            'module': target.module, 'profiles': list(target.profiles),
            'scopes': ['compile', 'runtime', 'test', 'provided', 'system'],
            'model_inputs': [{'path': logical, 'role': role, 'sha256': hashlib.sha256(model_inputs[logical]).hexdigest()}
                             for logical, role, _ in target.models], 'model_complete': False, 'profiles_complete': False,
            'tree_sha256': hashlib.sha256(raw).hexdigest(), 'scanner_refs': {}, 'declarations': {}, 'coverage_gaps': []}
        facts = prepare_maven_resolution(raw, canonical(sidecar), model_inputs=model_inputs, metadata=metadata)
        metadata['coverage_gaps'] = facts['inventory']['coverage_gaps']
        return LocalToolCompletion(metadata, facts)
