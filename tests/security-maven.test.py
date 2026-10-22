#!/usr/bin/env python3
"""Bounded Maven imports; host-event fixtures do not claim real Maven runs."""
from __future__ import annotations
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_fixture(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".test.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workflow = load_fixture("security-workflow")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def node(name, version="1.2.3", scope="compile", children=None):
    return {"groupId": "org.example", "artifactId": name, "version": version,
            "type": "jar", "classifier": "", "scope": scope, "optional": "false",
            "children": children or []}


def inputs():
    authority = workflow.make_authority(workflow.LiveHost())
    metadata = workflow.source_metadata(authority, "resolved", "inventory")
    metadata["tool_version"] = "3.11.0"
    models = {"pom.xml": b"<project>fixture parent</project>",
              "api/pom.xml": b"<project>fixture module</project>"}
    tree = node("api", scope="", children=[node("client"),
        node("support", children=[node("client", scope="runtime")]), node("test-client", scope="test")])
    raw = encoded(tree)
    sidecar = {"schema_version": 2,
        "producer": {"coordinate": "org.apache.maven.plugins:maven-dependency-plugin", "version": "3.11.0",
                     "format": "tree-json", "tool_sha256": "b" * 64},
        "module": "api", "profiles": ["local"], "scopes": ["compile", "runtime", "test"],
        "model_inputs": [{"path": path, "role": "module" if path.startswith("api/") else "parent",
                          "sha256": hashlib.sha256(content).hexdigest()} for path, content in models.items()],
        "model_complete": True, "profiles_complete": True, "tree_sha256": hashlib.sha256(raw).hexdigest(),
        "scanner_refs": {"0": {"path": "api/pom.xml", "package_id": "client-one"},
                         "1": {"path": "api/pom.xml", "package_id": "support-one"},
                         "1/0": {"path": "api/pom.xml", "package_id": "client-two"},
                         "2": {"path": "api/pom.xml", "package_id": "test-one"}},
        "declarations": {"0": "[1.0,2.0)"}, "coverage_gaps": []}
    return metadata, raw, sidecar, models


def register_resolution(dependencies, host, authority, port, ref, *, version='1.2.3', complete=True):
    """Real importer + original source registration, with an explicit test host."""
    from aek.adapters.security_maven import prepare_maven_resolution, register_maven_resolution
    from aek.adapters.security_evidence import source_binding
    metadata = workflow.source_metadata(authority, ref, 'inventory')
    metadata.update(tool_version='3.11.0', required_components=list(dependencies.REQUIRED))
    _, raw, sidecar, models = inputs()
    tree = json.loads(raw)
    tree['children'][0]['version'] = version
    raw = encoded(tree)
    sidecar.update(tree_sha256=hashlib.sha256(raw).hexdigest(), profiles_complete=complete)
    facts = prepare_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata)
    if not complete:
        metadata.update(coverage='PARTIAL', coverage_gaps=facts['inventory']['coverage_gaps'])
    binding = source_binding(metadata, facts)
    handle = authority.on_source_completed(host.issue(authority, 'tool-completed', 'source', binding, actor='tool'),
                                           binding=binding)
    register_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata,
                              source_port=port, handle=handle)
    return facts


class MavenImportTests(unittest.TestCase):
    def test_record_reinspection_does_not_discard_conflicting_resolution_or_reuse_subset_approval(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.dependencies import dependency_evidence_digest
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record
        dependencies = load_fixture('security-dependencies')
        for affected in (False, True):
            with self.subTest(affected=affected):
                payload, host, authority, port = dependencies.dependency_context()
                register_resolution(dependencies, host, authority, port, 'resolved')
                register_resolution(dependencies, host, authority, port, 'resolved-conflict', version='1.4.0')
                package = {'ecosystem': 'maven', 'package': 'org.example:client', 'version': '1.2.3'}
                dependencies.register_source(host, authority, port, 'advisory', 'advisory',
                    {**package, 'advisories': [dependencies.advisory_payload()] if affected else []})
                dependencies.register_source(host, authority, port, 'compatibility', 'compatibility',
                                             {**package, 'compatible': True})
                refs = ['resolved', 'advisory', 'compatibility']
                payload['proposals'][0].update(version='[1.0,2.0)', evidence_refs=refs[:])
                if affected:
                    observations = tuple({'metadata': port.lookup(ref).metadata, 'facts': port.lookup(ref).facts,
                        'provenance_ref': port.lookup(ref).provenance_ref} for ref in refs)
                    binding = {'operation': 'accept-risk', 'dependency_ref': 'DEP-1', 'version': '1.2.3',
                        'scope_ref': authority.scope_ref, 'identity_digest': authority.current_identity.envelope_digest,
                        'expires_at': authority.expires_at, 'evidence_digest': dependency_evidence_digest(observations)}
                    event = host.issue(authority, 'user-confirmation', 'accept-risk', binding, actor='k')
                    handle = authority.on_user_confirmation(event, binding=binding)
                    approval = authority.validate(handle, operation='accept-risk', binding=binding)['provenance_ref']
                    payload['proposals'][0]['evidence_refs'].append(approval)
                body = record_as_dict(new_security_record(encoded(payload), review_id='REVIEW-CONFLICT'))
                body['sources'] = [port.lookup(ref).metadata for ref in refs + ['resolved-conflict']]
                key = 'checks:dependency-design-check'
                result = SecurityReviewService(source_port=port, approval_port=authority,
                    clock=lambda: dependencies.NOW).inspect_evidence(seal_security_record(body),
                    current_identity=authority.current_identity, required_components=dependencies.REQUIRED,
                    requirements=(key,))
                self.assertEqual(result[key]['status'], 'PENDING', result)
                self.assertIn('RESOLUTION_VERSION_AMBIGUOUS', result[key]['details']['reason_codes'])
                self.assertTrue({'resolved', 'resolved-conflict'} <= set(result[key]['evidence_refs']))

    def test_partial_resolution_cannot_be_dropped_or_unknown_regression_version_pass_delivery(self):
        from aek.core.security.dependencies import evaluate_design_dependency
        dependencies, delivery = load_fixture('security-dependencies'), load_fixture('security-delivery')
        payload, host, authority, port = dependencies.dependency_context()
        resolved = register_resolution(dependencies, host, authority, port, 'resolved')
        register_resolution(dependencies, host, authority, port, 'resolved-partial', complete=False)
        package = {'ecosystem': 'maven', 'package': 'org.example:client', 'version': '1.2.3'}
        dependencies.register_source(host, authority, port, 'advisory', 'advisory', {**package, 'advisories': []})
        dependencies.register_source(host, authority, port, 'compatibility', 'compatibility', {**package, 'compatible': True})
        refs = ['resolved', 'advisory', 'compatibility']
        proposal = {**payload['proposals'][0], 'version': '[1.0,2.0)', 'evidence_refs': refs}
        decision = evaluate_design_dependency(proposal, delivery.observations(port, refs),
            current_identity=authority.current_identity, required_components=dependencies.REQUIRED,
            scope_ref=authority.scope_ref, now=dependencies.NOW)
        self.assertEqual(decision['status'], 'ADOPTABLE')
        shipped = delivery.inventory(authority, kind='SHIPPED')
        delivery.register_inventory(host, authority, port, shipped)
        dependencies.register_source(host, authority, port, 'scope', 'delivery-scope', {'shipped_required': True})
        dependencies.register_source(host, authority, port, 'normal', 'regression',
                                     {'passed': True, 'dependency_ref': 'DEP-1', 'version': None})
        result = delivery.evaluate(decision, (resolved['inventory'], shipped), port, authority,
                                   refs + ['resolved-partial', 'shipped', 'scope', 'normal'])
        self.assertEqual(result['status'], 'PENDING', result)
        self.assertIn('DETERMINISTIC_RESOLUTION_REQUIRED', result['reason_codes'])
        self.assertIn('DEPENDENCY_REGRESSION_REQUIRED', result['reason_codes'])

    def test_service_dynamic_risk_disposition_is_bound_to_actual_version_and_current_resolution(self):
        from aek.adapters.security_maven import prepare_maven_resolution, register_maven_resolution
        from aek.adapters.security_evidence import source_binding
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.dependencies import dependency_evidence_digest
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record
        dependencies = load_fixture('security-dependencies')
        delivery = load_fixture('security-delivery')
        payload, host, authority, port = dependencies.dependency_context()
        metadata = workflow.source_metadata(authority, 'resolved', 'inventory')
        metadata.update(tool_version='3.11.0', required_components=list(dependencies.REQUIRED))
        _, raw, sidecar, models = inputs()
        facts = prepare_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata)
        binding = source_binding(metadata, facts)
        handle = authority.on_source_completed(host.issue(authority, 'tool-completed', 'source', binding, actor='tool'),
                                               binding=binding)
        register_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata,
                                  source_port=port, handle=handle)
        package = {'ecosystem': 'maven', 'package': 'org.example:client', 'version': '1.2.3'}
        dependencies.register_source(host, authority, port, 'advisory', 'advisory',
                                     {**package, 'advisories': [dependencies.advisory_payload()]})
        dependencies.register_source(host, authority, port, 'compatibility', 'compatibility',
                                     {**package, 'compatible': True})
        refs = ['resolved', 'advisory', 'compatibility']
        observed = tuple({'metadata': port.lookup(ref).metadata, 'facts': port.lookup(ref).facts,
                          'provenance_ref': port.lookup(ref).provenance_ref} for ref in refs)
        payload['proposals'][0].update(version='[1.0,2.0)', evidence_refs=refs)
        service = SecurityReviewService(source_port=port, approval_port=authority, clock=lambda: dependencies.NOW)
        kwargs = dict(current_identity=authority.current_identity, required_components=dependencies.REQUIRED)
        shipped = delivery.inventory(authority, kind='SHIPPED')
        delivery.register_inventory(host, authority, port, shipped)
        dependencies.register_source(host, authority, port, 'scope', 'delivery-scope', {'shipped_required': True})
        dependencies.register_source(host, authority, port, 'normal', 'regression',
                                     {'passed': True, 'dependency_ref': 'DEP-1', 'version': '1.2.3'})
        for version, expected in (('1.2.3', 'ADOPTABLE'), ('[1.0,2.0)', 'PENDING'), ('1.2.4', 'PENDING')):
            with self.subTest(approved_version=version):
                approval = {'operation': 'accept-risk', 'dependency_ref': 'DEP-1', 'version': version,
                    'scope_ref': authority.scope_ref, 'identity_digest': authority.current_identity.envelope_digest,
                    'expires_at': authority.expires_at, 'evidence_digest': dependency_evidence_digest(observed)}
                event = host.issue(authority, 'user-confirmation', 'accept-risk', approval, actor='k')
                handle = authority.on_user_confirmation(event, binding=approval)
                ref = authority.validate(handle, operation='accept-risk', binding=approval)['provenance_ref']
                payload['proposals'][0]['evidence_refs'] = refs + [ref]
                result = service.evaluate(encoded(payload), **kwargs)
                self.assertEqual(result['dependency_decisions'][0]['status'], expected, result)
                self.assertEqual(result['dependency_decisions'][0]['proposal']['version'], '[1.0,2.0)')
                payload['inventories'] = [facts['inventory'], shipped]
                body = record_as_dict(new_security_record(encoded(payload), review_id='REVIEW-MAVEN-RISK'))
                body['sources'] = [port.lookup(key).metadata for key in refs + ['shipped', 'scope', 'normal']]
                record = seal_security_record(body)
                requirements = ('checks:dependency-design-check', 'checks:dependency-delivery-check')
                inspected = service.inspect_evidence(record, requirements=requirements, **kwargs)
                expected_gate = 'PASS' if expected == 'ADOPTABLE' else 'PENDING'
                for key in requirements:
                    self.assertEqual(inspected[key]['status'], expected_gate, inspected)
        host.history.clear()
        self.assertEqual(service.evaluate(encoded(payload), **kwargs)['dependency_decisions'][0]['status'], 'PENDING')

    def test_service_record_reinspection_selects_actual_version_sources_and_rechecks_provenance(self):
        from aek.adapters.security_maven import prepare_maven_resolution, register_maven_resolution
        from aek.adapters.security_evidence import source_binding
        from aek.application.security_review import SecurityReviewService
        from aek.core.security.record import new_security_record, record_as_dict, seal_security_record
        dependencies = load_fixture('security-dependencies')
        payload, host, authority, port = dependencies.dependency_context()
        metadata = workflow.source_metadata(authority, 'resolved', 'inventory')
        metadata.update(tool_version='3.11.0', required_components=list(dependencies.REQUIRED))
        _, raw, sidecar, models = inputs()
        facts = prepare_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata)
        binding = source_binding(metadata, facts)
        handle = authority.on_source_completed(host.issue(authority, 'tool-completed', 'source', binding, actor='tool'),
                                               binding=binding)
        register_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata,
                                  source_port=port, handle=handle)
        payload['proposals'][0].update(version='[1.0,2.0)', evidence_refs=['resolved'])
        package = {'ecosystem': 'maven', 'package': 'org.example:client', 'version': '1.2.3'}
        dependencies.register_source(host, authority, port, 'advisory', 'advisory', {**package, 'advisories': []})
        dependencies.register_source(host, authority, port, 'compatibility', 'compatibility', {**package, 'compatible': True})
        body = record_as_dict(new_security_record(encoded(payload), review_id='REVIEW-MAVEN'))
        body['sources'] = [port.lookup(ref).metadata for ref in ('resolved', 'advisory', 'compatibility')]
        record = seal_security_record(body)
        key = 'checks:dependency-design-check'
        service = SecurityReviewService(source_port=port, clock=lambda: dependencies.NOW)
        kwargs = dict(current_identity=authority.current_identity, required_components=dependencies.REQUIRED,
                      requirements=(key,))
        result = service.inspect_evidence(record, **kwargs)
        self.assertEqual(result[key]['status'], 'PASS', result)
        self.assertEqual(set(result[key]['evidence_refs']), {'resolved', 'advisory', 'compatibility'})
        self.assertEqual(record_as_dict(record)['dependency_decisions'][0]['proposal']['version'], '[1.0,2.0)')
        host.history.clear()
        self.assertEqual(service.inspect_evidence(record, **kwargs)[key]['status'], 'PENDING')

    def test_service_verifies_advisory_for_actual_dynamic_resolution_not_literal_range(self):
        from aek.adapters.security_maven import prepare_maven_resolution, register_maven_resolution
        from aek.adapters.security_evidence import source_binding
        from aek.application.security_review import SecurityReviewService
        dependencies = load_fixture('security-dependencies')
        payload, host, authority, port = dependencies.dependency_context()
        metadata = workflow.source_metadata(authority, 'resolved', 'inventory')
        metadata.update(tool_version='3.11.0', required_components=list(dependencies.REQUIRED))
        _, raw, sidecar, models = inputs()
        facts = prepare_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata)
        binding = source_binding(metadata, facts)
        handle = authority.on_source_completed(host.issue(authority, 'tool-completed', 'source', binding, actor='tool'),
                                               binding=binding)
        register_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata,
                                  source_port=port, handle=handle)
        payload['proposals'][0].update(version='[1.0,2.0)', evidence_refs=['resolved', 'advisory'])
        dependencies.register_source(host, authority, port, 'advisory', 'advisory', {
            'ecosystem': 'maven', 'package': 'org.example:client', 'version': '1.2.3',
            'advisories': [dependencies.advisory_payload()]})
        payload['findings'] = [{**dependencies.model.finding_payload(), 'category': 'DEPENDENCY',
            'dependency_ref': 'DEP-1', 'advisory_refs': ['GHSA-fixture-0001'], 'evidence_refs': ['advisory']}]
        service = SecurityReviewService(source_port=port, clock=lambda: dependencies.NOW)
        kwargs = dict(current_identity=authority.current_identity, required_components=dependencies.REQUIRED)
        result = service.evaluate(encoded(payload), **kwargs)
        self.assertEqual(result['findings'][0]['verification_status'], 'STATIC_VERIFIED')
        self.assertEqual(result['dependency_decisions'][0]['proposal']['version'], '[1.0,2.0)')
        payload['proposals'][0]['evidence_refs'] = ['advisory']
        self.assertNotEqual(service.evaluate(encoded(payload), **kwargs)['findings'][0]['verification_status'],
                            'STATIC_VERIFIED')
        # An advisory claiming a range cannot stand in for exact resolution.
        dependencies.register_source(host, authority, port, 'range-advisory', 'advisory', {
            'ecosystem': 'maven', 'package': 'org.example:client', 'version': '[1.0,2.0)',
            'advisories': [dependencies.advisory_payload()]})
        payload['findings'][0]['evidence_refs'] = ['range-advisory']
        self.assertNotEqual(service.evaluate(encoded(payload), **kwargs)['findings'][0]['verification_status'],
                            'STATIC_VERIFIED')

    def test_invalid_model_path_and_role_have_bounded_input_errors(self):
        from aek.adapters.security_maven import prepare_maven_resolution
        from aek.core.security.model import SecurityInputError
        for key in ('path', 'role'):
            with self.subTest(key=key):
                metadata, raw, sidecar, models = inputs()
                sidecar['model_inputs'][0][key] = ['invalid']
                with self.assertRaises(SecurityInputError):
                    prepare_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata)

    def test_root_module_uses_canonical_dot_without_allowing_model_path_traversal(self):
        from aek.adapters.security_maven import prepare_maven_resolution
        metadata, raw, sidecar, models = inputs()
        root_sidecar = {**sidecar, "module": "."}
        facts = prepare_maven_resolution(raw, encoded(root_sidecar), model_inputs=models, metadata=metadata)
        self.assertEqual(facts["module"], ".")
        self.assertEqual(facts["occurrences"][0]["module"], ".")
        for module in ("./api", "a/..", "..", "/tmp/api", "a//b", "C:api"):
            with self.subTest(module=module), self.assertRaises(ValueError):
                prepare_maven_resolution(raw, encoded({**sidecar, "module": module}), model_inputs=models, metadata=metadata)

    def test_partial_registration_cannot_be_relabeled_complete_or_replace_existing_ref(self):
        from aek.adapters.security_evidence import BuiltinSourceEvidencePort, source_binding
        from aek.adapters.security_maven import prepare_maven_resolution, register_maven_resolution
        from aek.core.security.authority import SecurityAuthorityError
        metadata, raw, sidecar, models = inputs()
        host = workflow.LiveHost()
        authority = workflow.make_authority(host)
        port = BuiltinSourceEvidencePort(authority)
        sidecar["profiles_complete"] = False
        partial = prepare_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata)
        def handle_for(value, facts):
            binding = source_binding(value, facts)
            return authority.on_source_completed(host.issue(authority, "tool-completed", "source", binding, actor="tool"),
                                                  binding=binding)
        with self.assertRaises(ValueError):
            register_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata,
                source_port=port, handle=handle_for(metadata, partial))
        self.assertIsNone(port.lookup("resolved"))
        partial_metadata = {**metadata, "coverage": "PARTIAL", "coverage_gaps": partial["inventory"]["coverage_gaps"]}
        registered = register_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=partial_metadata,
            source_port=port, handle=handle_for(partial_metadata, partial))
        self.assertEqual(registered.metadata["status"], "VERIFIED")
        self.assertEqual(registered.metadata["coverage"], "PARTIAL")
        sidecar["profiles_complete"] = True
        complete = prepare_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata)
        with self.assertRaises(SecurityAuthorityError):
            register_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata,
                source_port=port, handle=handle_for(metadata, complete))
        self.assertEqual(port.lookup("resolved").facts, partial)

    def test_size_depth_and_occurrence_limits_reject_instead_of_truncating_proof(self):
        from aek.adapters.security_maven import prepare_maven_resolution
        metadata, _, sidecar, models = inputs()
        trees = [node("api", scope="", children=[node("client") for _ in range(129)])]
        deep = node("client")
        for number in range(34):
            deep = node("ancestor-" + str(number), children=[deep])
        trees.append(node("api", scope="", children=[deep]))
        for tree in trees:
            raw = encoded(tree)
            with self.assertRaises(ValueError):
                prepare_maven_resolution(raw, encoded({**sidecar, "tree_sha256": hashlib.sha256(raw).hexdigest()}),
                                          model_inputs=models, metadata=metadata)

    def test_trusted_exact_resolution_allows_dynamic_design_only_with_actual_version_checks(self):
        from aek.adapters.security_maven import prepare_maven_resolution, register_maven_resolution
        from aek.adapters.security_evidence import source_binding
        from aek.core.security.dependencies import evaluate_design_dependency
        dependencies = load_fixture("security-dependencies")
        payload, host, authority, port = dependencies.dependency_context()
        metadata = workflow.source_metadata(authority, "resolved", "inventory")
        metadata["tool_version"] = "3.11.0"
        metadata["required_components"] = list(dependencies.REQUIRED)
        _, raw, sidecar, models = inputs()
        facts = prepare_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata)
        binding = source_binding(metadata, facts)
        handle = authority.on_source_completed(host.issue(authority, "tool-completed", "source", binding, actor="tool"),
                                               binding=binding)
        register_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata, source_port=port, handle=handle)
        proposal = {**payload["proposals"][0], "version": "[1.0,2.0)"}
        package = {"ecosystem": "maven", "package": "org.example:client", "version": "1.2.3"}
        dependencies.register_source(host, authority, port, "advisory", "advisory", {**package, "advisories": []})
        dependencies.register_source(host, authority, port, "compatibility", "compatibility", {**package, "compatible": True})
        def evaluate():
            observations = tuple({"metadata": port.lookup(ref).metadata, "facts": port.lookup(ref).facts,
                                  "provenance_ref": port.lookup(ref).provenance_ref}
                                 for ref in ("advisory", "compatibility", "resolved"))
            return evaluate_design_dependency(proposal, observations, current_identity=authority.current_identity,
                required_components=dependencies.REQUIRED, scope_ref=authority.scope_ref, now=dependencies.NOW)
        result = evaluate()
        self.assertEqual(result["status"], "ADOPTABLE")
        self.assertEqual(result["proposal"]["version"], "[1.0,2.0)")
        self.assertIn("resolved", result["source_refs"])
        self.assertIn("SHIPPED_INVENTORY_IF_APPLICABLE", result["pending_delivery_requirements"])
        host.history.clear()
        self.assertEqual(evaluate()["status"], "PENDING")

    def test_only_original_live_source_event_registers_and_restart_rechecks_provenance(self):
        from aek.adapters.security_authority import BuiltinWorkflowAuthority
        from aek.adapters.security_evidence import BuiltinSourceEvidencePort, source_binding
        from aek.adapters.security_maven import prepare_maven_resolution, register_maven_resolution
        from aek.core.security.authority import SecurityAuthorityError
        metadata, raw, sidecar, models = inputs()
        host = workflow.LiveHost()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "provenance").mkdir()
            (root / "sources").mkdir()
            original = workflow.make_authority(host)
            kwargs = dict(repository_ref=directory, task_id="task", scope_ref="scope",
                          current_identity=original.current_identity, author="author", host_source=host,
                          provenance_root=root / "provenance")
            authority = BuiltinWorkflowAuthority(**kwargs)
            port = BuiltinSourceEvidencePort(authority, root=root / "sources")
            facts = prepare_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata)
            with self.assertRaises(SecurityAuthorityError):
                register_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata,
                                          source_port=port, handle={"verified": True, "producer": "tool"})
            self.assertIsNone(port.lookup("resolved"))
            binding = source_binding(metadata, facts)
            token = host.issue(authority, "tool-completed", "source", binding, actor="tool")
            handle = authority.on_source_completed(token, binding=binding)
            observation = register_maven_resolution(raw, encoded(sidecar), model_inputs=models,
                metadata=metadata, source_port=port, handle=handle)
            self.assertEqual(observation.metadata["status"], "VERIFIED")
            restarted = BuiltinSourceEvidencePort(BuiltinWorkflowAuthority(**kwargs), root=root / "sources")
            self.assertEqual(restarted.lookup("resolved").metadata["status"], "VERIFIED")
            host.history.clear()
            self.assertEqual(restarted.lookup("resolved").metadata["status"], "DECLARED")
            self.assertEqual(restarted.lookup("resolved").facts, facts)

    def test_raw_sidecar_cannot_supply_authority_and_invalid_inputs_are_rejected(self):
        from aek.adapters.security_maven import prepare_maven_resolution
        metadata, raw, sidecar, models = inputs()
        for update in ({"status": "VERIFIED"}, {"handle": {"actor": "tool"}},
                       {"tree_sha256": "0" * 64}, {"module": "../outside"},
                       {"producer": {**sidecar["producer"], "version": "3.10.0"}},
                       {"scanner_refs": {"0": {"path": "C:/outside", "package_id": "x"}}},
                       {"scanner_refs": {"9": {"path": "api/pom.xml", "package_id": "x"}}}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                prepare_maven_resolution(raw, encoded({**sidecar, **update}), model_inputs=models, metadata=metadata)
        for changed in ({**models, "api/pom.xml": b"changed"}, {"pom.xml": models["pom.xml"]}):
            with self.assertRaises(ValueError):
                prepare_maven_resolution(raw, encoded(sidecar), model_inputs=changed, metadata=metadata)
        for changed in (b'{"groupId":"first","groupId":"second"}', b'{}\xff', b'{"x":NaN}', b" " * 2_000_001):
            with self.assertRaises(ValueError):
                prepare_maven_resolution(changed, encoded(sidecar), model_inputs=models, metadata=metadata)

    def test_missing_model_profiles_mapping_and_dynamic_tree_versions_are_not_complete(self):
        from aek.adapters.security_maven import prepare_maven_resolution
        metadata, raw, sidecar, models = inputs()
        for field, value, reason in (("model_complete", False, "MAVEN_MODEL_CLOSURE_UNKNOWN"),
                                     ("profiles_complete", False, "MAVEN_ACTIVE_PROFILES_UNKNOWN"),
                                     ("scanner_refs", {}, "MAVEN_SCANNER_MAPPING_MISSING:0")):
            with self.subTest(field=field):
                facts = prepare_maven_resolution(raw, encoded({**sidecar, field: value}), model_inputs=models, metadata=metadata)
                self.assertEqual(facts["inventory"]["coverage"], "PARTIAL")
                self.assertIn(reason, facts["inventory"]["coverage_gaps"])
        for version in ("LATEST", "RELEASE", "[1.0,2.0)", "${client.version}"):
            tree = json.loads(raw)
            tree["children"][0]["version"] = version
            changed = encoded(tree)
            facts = prepare_maven_resolution(changed, encoded({**sidecar,
                "tree_sha256": hashlib.sha256(changed).hexdigest()}), model_inputs=models, metadata=metadata)
            self.assertIsNone(facts["occurrences"][0]["resolved_version"])
            self.assertIsNone(facts["inventory"]["packages"][0]["version"])
            self.assertIn("MAVEN_VERSION_UNRESOLVED:0", facts["inventory"]["coverage_gaps"])

    def test_module_profile_and_model_changes_do_not_reuse_instance_ids(self):
        from aek.adapters.security_maven import prepare_maven_resolution
        metadata, raw, sidecar, models = inputs()
        first = prepare_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata)
        for update in ({"module": "other"}, {"profiles": ["prod"]},
                       {"producer": {**sidecar["producer"], "tool_sha256": "c" * 64}}):
            facts = prepare_maven_resolution(raw, encoded({**sidecar, **update}), model_inputs=models, metadata=metadata)
            self.assertNotEqual(first["occurrences"][0]["instance_id"], facts["occurrences"][0]["instance_id"])

    def test_resolution_ambiguity_incompatible_and_affected_actual_versions_remain_blocked(self):
        from aek.adapters.security_maven import prepare_maven_resolution
        from aek.core.security.dependencies import evaluate_design_dependency
        dependencies = load_fixture("security-dependencies")
        for mode in ("ambiguous", "incompatible", "affected", "stale", "future"):
            with self.subTest(mode=mode):
                payload, host, authority, port = dependencies.dependency_context()
                metadata = workflow.source_metadata(authority, "resolved", "inventory")
                metadata["tool_version"] = "3.11.0"
                metadata["required_components"] = list(dependencies.REQUIRED)
                _, raw, sidecar, models = inputs()
                if mode == "ambiguous":
                    tree = json.loads(raw)
                    tree["children"][1]["children"][0]["version"] = "1.4.0"
                    raw = encoded(tree)
                    sidecar["tree_sha256"] = hashlib.sha256(raw).hexdigest()
                    sidecar["declarations"]["1/0"] = "[1.0,2.0)"
                facts = prepare_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata)
                dependencies.register_source(host, authority, port, "resolved", "inventory", facts, tool_version="3.11.0")
                proposal = {**payload["proposals"][0], "version": "[1.0,2.0)"}
                package = {"ecosystem": "maven", "package": "org.example:client", "version": "1.2.3"}
                dependencies.register_source(host, authority, port, "advisory", "advisory",
                    {**package, "advisories": [dependencies.advisory_payload()] if mode == "affected" else []})
                dependencies.register_source(host, authority, port, "compatibility", "compatibility",
                    {**package, "compatible": mode != "incompatible"})
                observations = [{"metadata": port.lookup(ref).metadata, "facts": port.lookup(ref).facts,
                                 "provenance_ref": port.lookup(ref).provenance_ref}
                                for ref in ("advisory", "compatibility", "resolved")]
                if mode == "stale":
                    observations[2]["metadata"]["checked_at"] = "2026-09-01T00:00:00+00:00"
                if mode == "future":
                    observations[2]["metadata"]["checked_at"] = "2027-01-01T00:00:00+00:00"
                result = evaluate_design_dependency(proposal, tuple(observations), current_identity=authority.current_identity,
                    required_components=dependencies.REQUIRED, scope_ref=authority.scope_ref, now=dependencies.NOW)
                self.assertEqual(result["status"], "REJECTED" if mode == "incompatible" else "PENDING")
                if mode == "ambiguous":
                    self.assertIn("RESOLUTION_VERSION_AMBIGUOUS", result["reason_codes"])
                if mode == "affected":
                    self.assertIn("KNOWN_AFFECTED_DEPENDENCY", result["reason_codes"])

    def test_cycle_and_unknown_nodes_remain_partial_without_losing_occurrences(self):
        from aek.adapters.security_maven import prepare_maven_resolution
        metadata, raw, sidecar, models = inputs()
        tree = json.loads(raw)
        tree["children"][1]["children"][0] = node("api", scope="runtime")
        changed = encoded(tree)
        sidecar["tree_sha256"] = hashlib.sha256(changed).hexdigest()
        facts = prepare_maven_resolution(changed, encoded(sidecar), model_inputs=models, metadata=metadata)
        self.assertEqual(len(facts["occurrences"]), 4)
        self.assertEqual(facts["inventory"]["coverage"], "PARTIAL")
        self.assertIn("MAVEN_CYCLE_OR_REPEATED_ANCESTOR:1/0", facts["inventory"]["coverage_gaps"])
        tree["children"][0]["omitted"] = "conflict"
        changed = encoded(tree)
        sidecar["tree_sha256"] = hashlib.sha256(changed).hexdigest()
        facts = prepare_maven_resolution(changed, encoded(sidecar), model_inputs=models, metadata=metadata)
        self.assertIn("MAVEN_NODE_FIELDS_UNSUPPORTED:0", facts["inventory"]["coverage_gaps"])

    def test_root_artifact_and_optional_flag_are_bound_to_occurrence_identity(self):
        from aek.adapters.security_maven import prepare_maven_resolution
        metadata, raw, sidecar, models = inputs()
        first = prepare_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata)
        tree = json.loads(raw)
        tree["artifactId"] = "different-module-artifact"
        changed = encoded(tree)
        sidecar["tree_sha256"] = hashlib.sha256(changed).hexdigest()
        second = prepare_maven_resolution(changed, encoded(sidecar), model_inputs=models, metadata=metadata)
        self.assertNotEqual(first["occurrences"][0]["instance_id"], second["occurrences"][0]["instance_id"])
        tree = json.loads(raw)
        tree["children"][0]["optional"] = "true"
        changed = encoded(tree)
        sidecar["tree_sha256"] = hashlib.sha256(changed).hexdigest()
        third = prepare_maven_resolution(changed, encoded(sidecar), model_inputs=models, metadata=metadata)
        self.assertNotEqual(first["occurrences"][0]["instance_id"], third["occurrences"][0]["instance_id"])
        self.assertTrue(third["occurrences"][0]["optional"])

    def test_repeated_coordinates_preserve_each_path_scope_and_declaration(self):
        from aek.adapters.security_maven import prepare_maven_resolution
        metadata, raw, sidecar, models = inputs()
        facts = prepare_maven_resolution(raw, encoded(sidecar), model_inputs=models, metadata=metadata)
        self.assertEqual(facts["inventory"]["kind"], "RESOLVED")
        self.assertEqual(facts["inventory"]["coverage"], "COMPLETE_FOR_DECLARED_SCOPE")
        self.assertEqual(len(facts["occurrences"]), 4)
        first, support, second, test = facts["occurrences"]
        self.assertEqual((first["package"], second["package"]), ("org.example:client", "org.example:client"))
        self.assertNotEqual(first["instance_id"], second["instance_id"])
        self.assertEqual(first["parent_path"], [])
        self.assertEqual(second["parent_path"], [support["instance_id"]])
        self.assertEqual((first["scope"], second["scope"], test["scope"]), ("compile", "runtime", "test"))
        self.assertEqual(first["declared_version"], "[1.0,2.0)")
        self.assertEqual(first["resolved_version"], "1.2.3")
        self.assertEqual(second["scanner_ref"], {"path": "api/pom.xml", "package_id": "client-two"})
        package = facts["inventory"]["packages"][2]
        self.assertEqual(package["relationship"], "transitive")
        self.assertEqual(package["introduced_by"], [support["instance_id"]])
        self.assertEqual(package["dependency_path"], [support["instance_id"], second["instance_id"]])
        self.assertEqual(facts["tree_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertNotIn("status", facts)


if __name__ == "__main__":
    unittest.main()
