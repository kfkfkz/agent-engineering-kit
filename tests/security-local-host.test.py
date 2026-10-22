"""Local host orchestration protocol, not real tools or production approval."""
from pathlib import Path
import copy
import importlib.util
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

spec = importlib.util.spec_from_file_location("workflow_fixtures", Path(__file__).with_name("security-workflow.test.py"))
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


class ProtocolHost(workflow.LiveHost):
    """Only protocol substitute; actual signed journals/TTY are tested elsewhere."""
    def __init__(self):
        super().__init__()
        self.attempts, self.recoveries = {}, {}
        self.approvals, self.begins, self.completions = 0, 0, 0
        self.interactive = True

    def approve_execution(self, authority, *, plan, binding, reviewer, revalidate):
        from aek.core.security.authority import SecurityAuthorityError
        if not self.interactive:
            raise SecurityAuthorityError("INTERACTIVE_TERMINAL_REQUIRED")
        if revalidate() is not True:
            raise SecurityAuthorityError("HOST_PLAN_STALE")
        self.approvals += 1
        token = self.issue(authority, "user-confirmation", "execute", binding, actor=reviewer)
        return authority.on_user_confirmation(token, binding=binding)

    def begin_attempt(self, authority, *, plan, binding, grant):
        from aek.core.security.authority import digest, SecurityAuthorityError
        authority.validate_runtime_grant(grant, **{k: v for k, v in binding.items() if k != "operation"})
        key = digest(binding)
        if key in self.attempts:
            raise SecurityAuthorityError("HOST_ATTEMPT_ALREADY_STARTED")
        self.begins += 1
        self.attempts[key] = {"status": "UNKNOWN", "plan": copy.deepcopy(plan)}
        return key

    def _handle(self, authority, metadata, facts):
        from aek.adapters.security_evidence import source_binding
        binding = source_binding(metadata, facts)
        token = self.issue(authority, "tool-completed", "source", binding, actor=metadata["producer_ref"])
        return authority.on_source_completed(token, binding=binding)

    def complete_attempt(self, authority, attempt, *, metadata, facts, revalidate):
        from aek.core.security.authority import SecurityAuthorityError
        if revalidate() is not True:
            raise SecurityAuthorityError("HOST_PLAN_STALE")
        self.completions += 1
        self.attempts[attempt].update(status="COMPLETED", metadata=copy.deepcopy(metadata), facts=copy.deepcopy(facts))
        return self._handle(authority, metadata, facts)

    def restore_attempt(self, authority, *, plan, binding, revalidate):
        from aek.core.security.authority import digest, SecurityAuthorityError
        if revalidate() is not True:
            raise SecurityAuthorityError("HOST_PLAN_STALE")
        state = self.attempts.get(digest(binding))
        if state is None:
            return None
        if state["status"] != "COMPLETED":
            return {"status": "UNKNOWN"}
        token = object()
        self.recoveries[token] = state
        return {"status": "COMPLETED", "metadata": copy.deepcopy(state["metadata"]),
                "facts": copy.deepcopy(state["facts"]), "handle": None, "completion_token": token}

    def recover_source(self, authority, completion_token, *, revalidate):
        from aek.core.security.authority import SecurityAuthorityError
        if revalidate() is not True:
            raise SecurityAuthorityError("HOST_PLAN_STALE")
        state = self.recoveries.pop(completion_token)
        return self._handle(authority, state["metadata"], state["facts"])


class ProtocolProvider:
    def __init__(self, authority):
        self.authority, self.runs, self.current = authority, 0, True
        self.failure = None

    def prepare(self, data):
        from aek.application.local_host import PreparedLocalTool
        from aek.core.security.authority import digest, runtime_binding
        if not set(data["targets"]) <= {"app.py"}:
            raise ValueError("target is outside fixed profile")
        plan = {"tool_id": "protocol-tool", "targets": data["targets"], "runtime_sha256": "b" * 64}
        binding = runtime_binding(plan_digest=digest(plan), repository_ref=self.authority.repository_ref,
            scope_ref=self.authority.scope_ref, identity_digest=self.authority.current_identity.envelope_digest,
            tool_id="protocol-tool", executable_digest="b" * 64, action="scan", network_mode="deny",
            write_scope=("private-output",), timeout_seconds=1, max_output_bytes=10000, max_items=100)
        return PreparedLocalTool(plan, binding, "source-" + digest(binding), provider_data=object())

    def revalidate(self, prepared):
        return self.current

    def execute(self, prepared, grant, authority):
        from aek.application.local_host import LocalToolCompletion
        authority.validate_runtime_grant(grant, **{k: v for k, v in prepared.binding.items() if k != "operation"})
        self.runs += 1
        if self.failure:
            raise self.failure
        metadata = workflow.source_metadata(authority, prepared.source_ref, "protocol-completion")
        metadata["producer_ref"] = "protocol-tool"
        return LocalToolCompletion(metadata, {"plan_digest": prepared.binding["plan_digest"], "status": "PASSED"})


def fixture():
    from aek.adapters.security_evidence import BuiltinSourceEvidencePort
    from aek.application.local_host import LocalToolHost
    host = ProtocolHost()
    authority = workflow.make_authority(host)
    port = BuiltinSourceEvidencePort(authority)
    provider = ProtocolProvider(authority)
    service = LocalToolHost(host_source=host, authority=authority, source_port=port,
                           providers={"test-local": provider})
    return service, host, authority, port, provider


DATA = {"profile_id": "test-local", "targets": ["app.py"]}


class LocalHostTests(unittest.TestCase):
    def test_missing_fixed_provider_is_unavailable_not_an_execution(self):
        from aek.application.local_host import LocalToolHost
        host = LocalToolHost(host_source=None, authority=None, source_port=None, providers={})
        result = host.execute({"profile_id": "not-installed", "targets": ["pom.xml"]}, reviewer="k")
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["reason_codes"], ["PROVIDER_UNAVAILABLE"])

    def test_fixed_provider_gets_original_grant_and_registers_original_source(self):
        service, host, _, port, provider = fixture()
        prepared = service.prepare(DATA)
        self.assertEqual(prepared["status"], "PREPARED")
        self.assertEqual(provider.runs, 0)
        self.assertEqual(host.approvals, 0)
        result = service.execute(DATA, reviewer="k")
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual((provider.runs, host.approvals, host.begins, host.completions), (1, 1, 1, 1))
        self.assertEqual(port.lookup(result["source_ref"]).metadata["status"], "VERIFIED")

    def test_failed_native_attempt_stays_unknown_without_an_automatic_second_execution(self):
        service, host, _, _, provider = fixture()
        provider.failure = RuntimeError('controlled provider failure')
        first = service.execute(DATA, reviewer='protocol-test')
        self.assertEqual(first['status'], 'INCOMPLETE')
        provider.failure = None
        second = service.execute(DATA, reviewer='protocol-test')
        self.assertEqual(second['reason_codes'], ['NATIVE_EXECUTION_RESULT_UNKNOWN'])
        self.assertEqual((provider.runs, host.approvals, host.begins, host.completions), (1, 1, 1, 0))

    def test_registration_failure_recovers_the_completion_and_then_reuses_without_reapproval(self):
        service, host, _, port, provider = fixture()
        with patch.object(port, 'register', side_effect=OSError('controlled registration failure')):
            first = service.execute(DATA, reviewer='protocol-test')
        self.assertEqual(first['reason_codes'], ['SOURCE_REGISTRATION_INCOMPLETE'])
        restored = service.execute(DATA, reviewer='protocol-test')
        self.assertEqual((restored['status'], restored['action']), ('COMPLETED', 'recovered'))
        repeated = service.execute(DATA, reviewer='protocol-test')
        self.assertEqual((repeated['status'], repeated['action']), ('COMPLETED', 'reuse'))
        self.assertEqual((provider.runs, host.approvals, host.begins, host.completions), (1, 1, 1, 1))

    def test_untrusted_extra_fields_and_invalid_targets_never_reach_a_provider(self):
        from aek.core.security.authority import SecurityAuthorityError
        service, host, _, _, provider = fixture()
        for bad in ({**DATA, 'facts': {}}, {**DATA, 'actor': 'k'}, {**DATA, 'argv': ['sh']},
                    {**DATA, 'targets': []}, {**DATA, 'targets': ['app.py', 'app.py']},
                    {**DATA, 'targets': [[]]}, {**DATA, 'profile_id': '../../provider'}):
            with self.subTest(bad=bad), self.assertRaises(SecurityAuthorityError):
                service.execute(bad, reviewer='protocol-test')
        self.assertEqual((provider.runs, host.approvals), (0, 0))

    def test_noninteractive_and_revoked_sources_do_not_start_or_reexecute(self):
        from aek.core.security.authority import SecurityAuthorityError
        service, host, _, _, provider = fixture()
        host.interactive = False
        with self.assertRaisesRegex(SecurityAuthorityError, 'INTERACTIVE_TERMINAL_REQUIRED'):
            service.execute(DATA, reviewer='protocol-test')
        self.assertEqual(provider.runs, 0)
        host.interactive = True
        self.assertEqual(service.execute(DATA, reviewer='protocol-test')['status'], 'COMPLETED')
        host.history.clear()
        result = service.execute(DATA, reviewer='protocol-test')
        self.assertEqual(result['reason_codes'], ['HOST_COMPLETION_STALE'])
        self.assertEqual((provider.runs, host.approvals), (1, 1))


if __name__ == "__main__":
    unittest.main()
