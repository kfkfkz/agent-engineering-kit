"""Controller-selected Trivy host; native completion uses the original host.

No installation, DB update, imported completion trust or execution authority.
Linux namespaces and pinned read-only mounts implement this fixed boundary;
private snapshots do not resist arbitrary trusted same-UID controller code.
Cold adapters require their own original authorized version probe before this
provider can be configured. Existing signed scan sources remain independently
readable through ScannerAdapter.normalize_v2 without that ephemeral cache.
System /dev is a trusted root-owned base: fixed device names are observed via
open FDs, then dev-bound by name; this does not resist privileged replacement.
NPROC retains its UID/namespace semantics, not an aggregate process-tree budget.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from aek.adapters import ui_filesystem as fs
from aek.adapters.security_evidence import source_binding
from aek.adapters.security_process import ProcessPort
from aek.adapters.security_runtime import BoundRuntimeAdoption
from aek.adapters.security_scanner import ScannerAdapter, _ScannerCompletion
from aek.application.local_host import LocalToolCompletion, PreparedLocalTool
from aek.core.context.identity import identity_as_dict
from aek.core.security.authority import canonical, digest, runtime_binding, timestamp
from aek.core.security.scanners import ScannerPlan, ScanEvidencePolicy, VERSIONS, database_currentness


@dataclass(frozen=True)
class TrivyHostRuntime:
    trivy: str
    trivy_sha256: str
    bubblewrap: str
    bubblewrap_sha256: str
    prlimit: str
    prlimit_sha256: str
    readonly_roots: tuple[str, ...] = ('/usr/lib', '/usr/lib64')


def _open_binary(path):
    path = Path(path)
    parent = fs.open_absolute_dir(path.parent)
    try:
        return fs.open_file(parent, path.name, os.O_RDONLY)
    finally:
        fs.close_dir(parent)


def _measure(fd, destination=None):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 512 * 1024 * 1024:
        raise ValueError('TRIVY_RUNTIME_UNAVAILABLE')
    sha, total = hashlib.sha256(), 0
    while block := os.read(fd, 1024 * 1024):
        total += len(block)
        if total > 512 * 1024 * 1024:
            raise ValueError('TRIVY_RUNTIME_UNAVAILABLE')
        sha.update(block)
        if destination is not None:
            view = memoryview(block)
            while view:
                view = view[os.write(destination, view):]
    after = os.fstat(fd)
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if identity(before) != identity(after) or total != before.st_size:
        raise ValueError('TRIVY_RUNTIME_CHANGED')
    return sha.hexdigest()


def trivy_runtime_fingerprint(runtime):
    """Current bytes of the three fixed binaries; no probe or permission."""
    try:
        if (type(runtime) is not TrivyHostRuntime or sys.platform != 'linux'
                or runtime.readonly_roots != ('/usr/lib', '/usr/lib64')):
            return None
        for path, expected in ((runtime.trivy, runtime.trivy_sha256),
                (runtime.bubblewrap, runtime.bubblewrap_sha256), (runtime.prlimit, runtime.prlimit_sha256)):
            fd = _open_binary(path)
            try:
                if not os.fstat(fd).st_mode & 0o111 or _measure(fd) != expected:
                    return None
            finally:
                os.close(fd)
        for path in runtime.readonly_roots:
            fs.close_dir(fs.open_absolute_dir(Path(path)))
        return digest(asdict(runtime))
    except (OSError, TypeError, ValueError):
        return None


class LinuxTrivyProcessPort:
    supported_boundaries = frozenset({'network', 'filesystem', 'process_group'})

    def __init__(self, runtime, adoption):
        if type(runtime) is not TrivyHostRuntime or type(adoption) is not BoundRuntimeAdoption:
            raise ValueError('INVALID_FIXED_TRIVY_RUNTIME')
        self.runtime, self.adoption = runtime, adoption
        self._bound = (runtime, id(adoption), adoption._bound)

    def control_fence(self):
        return (self._bound == (self.runtime, id(self.adoption), self.adoption._bound)
                and self.adoption._snapshot() == self.adoption._bound)

    @contextmanager
    def _pin(self, root=None):
        import fcntl
        owned, binaries, roots, devices = [], [], [], []
        try:
            for path, expected in ((self.runtime.bubblewrap, self.runtime.bubblewrap_sha256),
                    (self.runtime.prlimit, self.runtime.prlimit_sha256), (self.runtime.trivy, self.runtime.trivy_sha256)):
                source = _open_binary(path)
                try:
                    fd = os.memfd_create('aek-trivy-runtime', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
                    owned.append(fd)
                    os.fchmod(fd, 0o500)
                    if _measure(source, fd) != expected:
                        raise ValueError('TRIVY_RUNTIME_CHANGED')
                    fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW |
                                fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
                    os.lseek(fd, 0, os.SEEK_SET)
                    binaries.append(fd)
                finally:
                    os.close(source)
            for path in (*self.runtime.readonly_roots, *((str(root),) if root is not None else ())):
                fd = fs.open_absolute_dir(Path(path))
                owned.append(fd)
                roots.append((path, fd))
            dev = fs.open_absolute_dir(Path('/dev'))
            try:
                root_fd = fs.open_absolute_dir(Path('/'))
                try:
                    for descriptor in (root_fd, dev):
                        info = os.fstat(descriptor)
                        if info.st_uid != 0 or info.st_mode & 0o022:
                            raise ValueError('TRIVY_RUNTIME_UNAVAILABLE')
                finally:
                    fs.close_dir(root_fd)
                for name, minor in (('null', 3), ('urandom', 9)):
                    fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=dev)
                    owned.append(fd)
                    info = os.fstat(fd)
                    if not stat.S_ISCHR(info.st_mode) or info.st_rdev != os.makedev(1, minor):
                        raise ValueError('TRIVY_RUNTIME_UNAVAILABLE')
                    devices.append((name, fd))
            finally:
                fs.close_dir(dev)
            yield tuple(owned), binaries, roots, devices
        finally:
            for fd in reversed(owned):
                os.close(fd)

    def run(self, argv, cwd, *, timeout_seconds, max_output_bytes, environment, output_file=None):
        if (not self.control_fence() or self.adoption(self.runtime) is not True
                or type(argv) is not tuple or not argv or argv[0] != self.runtime.trivy
                or output_file is not None):
            raise ValueError('TRIVY_RUNTIME_UNAVAILABLE')
        probe = argv[1:] == ('--version',)
        if not probe and (len(argv) < 2 or argv[1] != 'fs'):
            raise ValueError('TRIVY_RUNTIME_UNAVAILABLE')
        with self._pin(None if probe else Path(cwd).parent) as (fds, binaries, roots, devices):
            bwrap, prlimit, trivy = binaries
            if sum(os.fstat(fd).st_size for fd in (prlimit, trivy)) > 512 * 1024 * 1024:
                raise ValueError('TRIVY_RUNTIME_UNAVAILABLE')
            command = [f'/proc/self/fd/{bwrap}', '--unshare-user', '--unshare-net', '--unshare-pid',
                '--unshare-ipc', '--unshare-uts', '--die-with-parent', '--clearenv',
                '--size', '134217728', '--tmpfs', '/', '--proc', '/proc', '--dir', '/dev',
                '--size', '134217728', '--tmpfs', '/tmp', '--dir', '/home',
                '--size', '536870912', '--tmpfs', '/tool']
            for name, _fd in devices:
                command += ['--dev-bind', '/dev/' + name, '/dev/' + name]
            for path, fd in roots:
                command += ['--ro-bind-fd', str(fd), path]
            command += ['--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
                '--perms', '0500', '--file', str(prlimit), '/tool/prlimit',
                '--perms', '0500', '--file', str(trivy), '/tool/trivy', '--remount-ro', '/tool',
                '--chdir', '/tmp' if probe else str(cwd)]
            for key, value in {'HOME': '/home', 'TMP': '/tmp', 'TEMP': '/tmp', 'XDG_CONFIG_HOME': '/home',
                    'XDG_CACHE_HOME': '/tmp', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}.items():
                command += ['--setenv', key, value]
            command += ['--', '/tool/prlimit', '--as=2147483648', '--nproc=64', '--nofile=128',
                        '--cpu=300', '--fsize=' + str(max_output_bytes), '--', '/tool/trivy', *argv[1:]]
            if self.adoption(self.runtime) is not True or not self.control_fence():
                raise ValueError('TRIVY_RUNTIME_UNAVAILABLE')
            return ProcessPort().run(tuple(command), Path(cwd), timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes, environment={'LANG': 'C.UTF-8'}, pass_fds=fds)


class FixedTrivyHostProfile:
    def __init__(self, adapter, plan, runtime, adoption, source_port, *, resolved_inventory_ref,
                 database_evidence_refs, policy, clock, required_components, profile_id='trivy-fixed'):
        authority = getattr(source_port, 'authority', None)
        port = getattr(adapter, 'process_port', None)
        if (type(adapter) is not ScannerAdapter or type(plan) is not ScannerPlan
                or type(runtime) is not TrivyHostRuntime or type(adoption) is not BoundRuntimeAdoption
                or type(port) is not LinuxTrivyProcessPort or port.runtime != runtime or port.adoption is not adoption
                or authority is not adoption._authority or source_port is not adoption._source_port
                or clock is not authority._clock or clock is not adoption._clock
                or plan.tool_id != 'trivy' or plan.action != 'scan' or plan.tool_version != VERSIONS['trivy']
                or adapter._version_evidence != (plan.executable_digest, plan.tool_version)
                or plan.executable_files != ((runtime.trivy, runtime.trivy_sha256),)
                or plan.executable_prefix != (runtime.trivy,) or type(policy) is not ScanEvidencePolicy
                or type(required_components) is not tuple or not required_components
                or type(database_evidence_refs) is not tuple or not 1 <= len(database_evidence_refs) <= 2
                or len(set(database_evidence_refs)) != len(database_evidence_refs)
                or any(type(ref) is not str or not ref for ref in (resolved_inventory_ref, *database_evidence_refs))):
            raise ValueError('INVALID_FIXED_TRIVY_PROFILE')
        self._adapter, self._plan, self._runtime, self._adoption = adapter, plan, runtime, adoption
        self._source, self._authority, self._clock, self._required = source_port, authority, clock, required_components
        self._resolved, self._databases, self._policy, self._profile = resolved_inventory_ref, database_evidence_refs, policy, profile_id
        self._port = port
        self._bound = self._snapshot()

    def _snapshot(self):
        a = self._authority
        return (id(self._adapter), id(self._adapter.process_port), self._adapter._version_evidence, self._plan, self._runtime,
            id(self._adoption), id(self._source), id(self._source.authority), id(a), a.repository_ref,
            a.task_id, a.scope_ref, a.current_identity, a.author, a.expires_at, id(a.host_source),
            id(a._clock), id(self._clock), self._required, self._resolved, self._databases, self._policy, self._profile)

    def control_fence(self):
        return self._snapshot() == self._bound and self._port.control_fence()

    def _material(self):
        if not self.control_fence() or self._adoption(self._runtime) is not True:
            raise ValueError('TRIVY_PROVIDER_UNAVAILABLE')
        plan, authority = self._plan, self._authority
        self._adapter.validate_plan_material(plan)
        if (plan.repository_ref != authority.repository_ref or plan.scope_ref != authority.scope_ref
                or plan.identity_digest != authority.current_identity.envelope_digest):
            raise ValueError('TRIVY_PROVIDER_UNAVAILABLE')
        observations = []
        now = self._clock()
        for ref, kind in ((self._resolved, 'inventory'), *((ref, 'database') for ref in self._databases)):
            item = self._source.lookup(ref)
            if item is None:
                raise ValueError('TRIVY_SOURCE_UNAVAILABLE')
            metadata, facts = item.metadata, item.facts
            if (metadata['status'] != 'VERIFIED' or metadata['kind'] != kind
                    or metadata['identity']['envelope_digest'] != plan.identity_digest
                    or metadata['scope_ref'] != plan.scope_ref or timestamp(metadata['checked_at']) > now
                    or metadata['coverage'] != 'COMPLETE_FOR_DECLARED_SCOPE' or metadata['coverage_gaps']
                    or facts.get('schema_version') != 2):
                raise ValueError('TRIVY_SOURCE_UNAVAILABLE')
            if kind == 'inventory':
                inventory = facts['inventory']
                if (inventory['kind'] != 'RESOLVED' or inventory['scope_ref'] != plan.scope_ref
                        or inventory['identity_digest'] != plan.identity_digest
                        or inventory['coverage'] != 'COMPLETE_FOR_DECLARED_SCOPE' or inventory['coverage_gaps']
                        or type(facts['occurrences']) is not list):
                    raise ValueError('TRIVY_SOURCE_UNAVAILABLE')
            else:
                db = facts['database']
                self._adapter._validate_database(db, plan)
                if (database_currentness(db, policy=self._policy, now=now)['status'] != 'CURRENT'
                        or timestamp(db['updated_at']) > timestamp(metadata['checked_at'])):
                    raise ValueError('TRIVY_DATABASE_UNAVAILABLE')
            observations.append(item)
        requires_java = any(item.get('ecosystem') in {'maven', 'jar', 'pom'}
                            for item in observations[0].facts['occurrences'])
        requires_java |= any(Path(path).suffix.lower() in {'.jar', '.war', '.ear', '.class'} or
            Path(path).name in {'pom.xml', 'build.gradle', 'build.gradle.kts'} for path, _ in plan.input_files)
        kinds = [item.facts['database']['kind'] for item in observations[1:]]
        if set(kinds) != ({'vulnerability', 'java'} if requires_java else {'vulnerability'}) or len(set(kinds)) != len(kinds):
            raise ValueError('TRIVY_DATABASE_UNAVAILABLE')
        if self._adoption(self._runtime) is not True:
            raise ValueError('TRIVY_PROVIDER_UNAVAILABLE')
        for item in observations:
            current = self._source.lookup(item.metadata['ref'])
            if current != item:
                raise ValueError('TRIVY_SOURCE_CHANGED')
        final_now = self._clock()
        if (final_now < now or timestamp(authority.expires_at) <= final_now
                or any(database_currentness(item.facts['database'], policy=self._policy, now=final_now)['status'] != 'CURRENT'
                       for item in observations[1:]) or not self.control_fence()):
            raise ValueError('TRIVY_PROVIDER_UNAVAILABLE')
        return canonical([asdict(item) for item in observations]), requires_java

    def _binding(self):
        return runtime_binding(**{key: getattr(self._plan, key) for key in ('plan_digest', 'repository_ref',
            'scope_ref', 'identity_digest', 'tool_id', 'executable_digest', 'action', 'network_mode',
            'write_scope', 'timeout_seconds', 'max_output_bytes', 'max_items')})

    def _source_ref(self):
        return 'trivy-' + digest({'task': self._authority.task_id, 'plan': self._plan.plan_digest,
            'resolved': self._resolved, 'databases': self._databases, 'policy': asdict(self._policy)})

    def prepare(self, data):
        if (type(data) is not dict or set(data) != {'profile_id', 'targets'}
                or data['profile_id'] != self._profile
                or data['targets'] != [path for path, _ in self._plan.input_files]):
            raise ValueError('INVALID_FIXED_TRIVY_SELECTION')
        material = self._material()
        return PreparedLocalTool(json.loads(canonical(asdict(self._plan))), self._binding(), self._source_ref(), material)

    def revalidate(self, prepared):
        try:
            return (type(prepared) is PreparedLocalTool and prepared.provider_data == self._material()
                and canonical(prepared.plan) == canonical(asdict(self._plan))
                and canonical(prepared.binding) == canonical(self._binding())
                and prepared.source_ref == self._source_ref() and self.control_fence())
        except Exception:
            return False

    def execute(self, prepared, grant, authority):
        if authority is not self._authority or not self.revalidate(prepared):
            raise ValueError('TRIVY_PROVIDER_UNAVAILABLE')
        captured = []
        scan = self._adapter.execute(self._plan, grant=grant, authority=authority, completion_sink=captured.append)
        if (len(captured) != 1 or type(captured[0]) is not _ScannerCompletion
                or scan.status not in {'PASSED', 'FAILED', 'INCONCLUSIVE'} or not self.revalidate(prepared)):
            raise ValueError('TRIVY_COMPLETION_UNAVAILABLE')
        completed = captured[0]
        if (completed.plan_digest != self._plan.plan_digest or not completed.cleanup_complete
                or completed.reason_code != 'PROCESS_COMPLETED' or completed.exit_code not in {0, 10}
                or hashlib.sha256(completed.raw).hexdigest() != scan.output_digest):
            raise ValueError('TRIVY_COMPLETION_UNAVAILABLE')
        report_utf8 = completed.raw.decode('utf-8', errors='strict')
        if (report_utf8.encode('utf-8') != completed.raw
                or hashlib.sha256(report_utf8.encode('utf-8')).hexdigest() != scan.output_digest):
            raise ValueError('TRIVY_COMPLETION_UNAVAILABLE')
        # A report can discover Java archives beyond the resolved input hints.
        # Such discoveries require their original current Java DB observation.
        report = json.loads(completed.raw)
        requires_java = prepared.provider_data[1] or any(
            row.get('Type') in {'jar', 'pom', 'java', 'maven'} for row in report['Results'])
        if requires_java and not prepared.provider_data[1]:
            raise ValueError('TRIVY_DATABASE_UNAVAILABLE')
        self._adapter._validate_execution(self._plan, grant, authority)
        facts = {'schema_version': 2, 'tool_id': 'trivy', 'tool_version': self._plan.tool_version,
            'output_digest': scan.output_digest, 'exit_code': completed.exit_code, 'plan_digest': completed.plan_digest,
            'resolved_inventory_ref': self._resolved, 'database_evidence_refs': list(self._databases),
            'requires_java_db': requires_java, 'scope_root': completed.scope_root,
            'report_utf8': report_utf8,
            'status': scan.status, 'coverage_gaps': list(scan.coverage_gaps),
            'findings': list(scan.findings), 'packages': list(scan.packages)}
        metadata = {'id': prepared.source_ref, 'ref': prepared.source_ref, 'run_id': prepared.source_ref,
            'producer_ref': 'trivy', 'identity': identity_as_dict(authority.current_identity),
            'required_components': list(self._required), 'kind': 'scanner-run', 'status': 'DECLARED',
            'checked_at': self._clock().isoformat(), 'scope_ref': authority.scope_ref,
            'tool_version': self._plan.tool_version, 'rules_digest': None, 'database_digest': self._plan.database_digest,
            'coverage': 'PARTIAL' if scan.coverage_gaps else 'COMPLETE_FOR_DECLARED_SCOPE',
            'coverage_gaps': list(scan.coverage_gaps), 'summary': 'Original fixed Trivy execution; candidates retain verification status'}
        source_binding(metadata, facts)
        if not self.control_fence():
            raise ValueError('TRIVY_COMPLETION_UNAVAILABLE')
        return LocalToolCompletion(metadata, facts)
