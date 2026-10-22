"""Occurrence and database facts; no scanner installation or live target scan."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def occurrence(name="a", *, instance="I-a", module="app", package_id="a@1"):
    return {"instance_id": instance, "ecosystem": "maven", "package": name,
            "resolved_version": "1", "module": module, "scope": "compile",
            "classification": "production", "resolution_context": "c" * 64,
            "parent_path": [], "scanner_ref": {"path": "pom.xml", "package_id": package_id}}


def package(name="a", package_id="a@1"):
    return {"path": "pom.xml", "ecosystem": "jar", "package": name,
            "version": "1", "package_id": package_id}


def sources(root, now):
    """Only host events are a test boundary; original source/authority are real."""
    from aek.adapters.security_authority import BuiltinWorkflowAuthority
    from aek.adapters.security_evidence import BuiltinSourceEvidencePort, source_binding
    from aek.core.context.identity import build_identity, identity_as_dict
    from aek.core.security.authority import HostEvent, digest
    class Host:
        def __init__(self):
            self.live, self.history = {}, []
        def capabilities(self):
            return {"available": True, "mode": "controlled-test-host"}
        def consume(self, kind, token):
            event = self.live.pop(token)
            if event.kind != kind:
                raise ValueError("wrong event kind")
            self.history.append(event)
            return event
        def restore(self, event):
            return event in self.history
    host = Host()
    identity = build_identity({name: {"scope_digest": "a" * 64} for name in
                              ("subject", "artifact", "policy", "context", "evidence")})
    authority = BuiltinWorkflowAuthority(repository_ref=str(root), task_id="scan-v2", scope_ref="a" * 64,
        current_identity=identity, author="author", host_source=host, clock=lambda: now[0])
    port = BuiltinSourceEvidencePort(authority)
    def register(ref, kind, facts, **overrides):
        metadata = {"id": ref, "ref": ref, "run_id": None, "producer_ref": "producer",
            "identity": identity_as_dict(identity), "required_components": ["subject.scope_digest"],
            "kind": kind, "status": "DECLARED", "checked_at": now[0].isoformat(),
            "scope_ref": authority.scope_ref, "tool_version": None, "rules_digest": None,
            "database_digest": None, "coverage": "COMPLETE_FOR_DECLARED_SCOPE", "coverage_gaps": [],
            "summary": "Controlled observation"}
        metadata.update(overrides)
        binding = source_binding(metadata, facts)
        token = object()
        host.live[token] = HostEvent(ref, "tool-completed", "producer", authority.task_id,
            authority.repository_ref, authority.scope_ref, identity.envelope_digest, "source",
            digest(binding), now[0].isoformat(), (now[0] + timedelta(hours=1)).isoformat())
        handle = authority.on_source_completed(token, binding=binding)
        port.register(metadata, facts, handle)
    return port, register, identity.envelope_digest, host


def complete_fixture(root, *, occurrences=None, scanned=None, requires_java_db=False,
                     database_updated_at="2026-10-18T00:00:00+00:00"):
    from aek.adapters.security_scanner import ScannerAdapter
    from aek.core.security.scanners import ScanEvidencePolicy
    now = [datetime(2026, 10, 19, tzinfo=timezone.utc)]
    port, register, identity, host = sources(root, now)
    (root / "pom.xml").write_text("<project/>", encoding="utf-8")
    (root / "config.json").write_text("{}", encoding="utf-8")
    (root / "version.py").write_text("print('Version: 0.75.0')", encoding="utf-8")
    (root / "cache/db").mkdir(parents=True)
    (root / "cache/db/trivy.db").write_bytes(b"synthetic database bytes, not a real advisory database")
    (root / "cache/db/metadata.json").write_text(json.dumps({"Version": 2, "UpdatedAt":
        database_updated_at, "NextUpdate": "2099-01-01T00:00:00Z"}), encoding="utf-8")
    adapter = ScannerAdapter(root, "trivy", (str(Path(sys.executable).resolve()), str(root / "version.py")))
    spec = importlib.util.spec_from_file_location("scanner_test_helpers", Path(__file__).with_name("security-scanners.test.py"))
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    probe = adapter.plan(identity_digest=identity, canonical_scope_ref="a" * 64,
                         action="version_probe", required_boundaries=())
    authority, grant = helpers.grant_for(probe)
    assert adapter.version_probe(probe, grant=grant, authority=authority).version_compatible
    plan = adapter.plan(("pom.xml",), identity_digest=identity, canonical_scope_ref="a" * 64,
        configuration_path="config.json", cache_files=("cache/db/trivy.db", "cache/db/metadata.json"), required_boundaries=())
    inventory = {"kind": "RESOLVED", "scope_ref": "a" * 64, "identity_digest": identity,
        "coverage": "COMPLETE_FOR_DECLARED_SCOPE", "coverage_gaps": []}
    register("resolved", "inventory", {"schema_version": 2, "inventory": inventory,
        "occurrences": [occurrence()] if occurrences is None else occurrences})
    db = {"kind": "vulnerability", "source": "registry-approved", "schema": 2,
          "updated_at": database_updated_at, "path": "cache/db/trivy.db",
          "metadata_path": "cache/db/metadata.json", "sha256": hashlib.sha256((root / "cache/db/trivy.db").read_bytes()).hexdigest(),
          "metadata_sha256": hashlib.sha256((root / "cache/db/metadata.json").read_bytes()).hexdigest()}
    register("db", "database", {"schema_version": 2, "database": db})
    rows = [{"ID": "a@1", "Name": "a", "Version": "1"}] if scanned is None else scanned
    raw = json.dumps({"SchemaVersion": 2, "Results": [{"Target": "pom.xml", "Type": "jar", "Packages": rows}]}).encode()
    register("scan", "scanner-run", {"schema_version": 2, "tool_id": "trivy", "tool_version": "0.75.0",
        "plan_digest": plan.plan_digest, "output_digest": hashlib.sha256(raw).hexdigest(), "exit_code": 0,
        "resolved_inventory_ref": "resolved", "database_evidence_refs": ["db"], "requires_java_db": requires_java_db})
    kwargs = dict(exit_code=0, version="0.75.0", scope_paths=("pom.xml",), source_port=port,
        resolved_inventory_ref="resolved", scan_evidence_ref="scan", database_evidence_refs=("db",),
        identity_digest=identity, scope_ref="a" * 64, plan=plan,
        policy=ScanEvidencePolicy("policy-current", 7, (("vulnerability", "registry-approved", 2),)), clock=lambda: now[0])
    return adapter, raw, kwargs, now, register, host


class CoverageTests(unittest.TestCase):
    def test_service_v2_uses_control_selected_policy_source_scope_and_current_clock(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.context.identity import decode_identity
        from aek.core.security.policy import SecurityPolicy
        with tempfile.TemporaryDirectory() as directory:
            adapter, raw, kwargs, now, _, _ = complete_fixture(Path(directory),
                database_updated_at="2026-10-17T00:00:00+00:00")
            current = decode_identity(kwargs['source_port'].lookup('resolved').metadata['identity'])
            service = SecurityReviewService(source_port=kwargs['source_port'],
                scanner_ports={'trivy': adapter}, policy=SecurityPolicy(max_advisory_age_days=1),
                scan_evidence_policy=kwargs['policy'], clock=lambda: now[0])
            def inspect():
                return service.inspect_scan_v2('trivy', raw, plan=kwargs['plan'], current_identity=current,
                    exit_code=0, resolved_inventory_ref='resolved', scan_evidence_ref='scan',
                    database_evidence_refs=('db',))
            result = inspect()
            self.assertEqual(result['schema_version'], 2)
            self.assertEqual(result['database_currentness']['status'], 'CURRENT')
            self.assertEqual(result['execution']['status'], 'VERIFIED')
            self.assertEqual(result['scan']['status'], 'PASSED')
            self.assertEqual(result['final_gate'], 'NOT_EVALUATED')
            now[0] += timedelta(days=8)
            self.assertNotEqual(inspect()['scan']['status'], 'PASSED')

    def test_service_missing_database_policy_keeps_existing_findings_and_unknown_dimensions(self):
        from aek.application.security_review import SecurityReviewService
        from aek.core.context.identity import decode_identity
        with tempfile.TemporaryDirectory() as directory:
            adapter, raw, kwargs, _, _, _ = complete_fixture(Path(directory))
            report = json.loads(raw)
            report['Results'][0]['Vulnerabilities'] = [{
                'VulnerabilityID': 'GHSA-existing-risk', 'PkgName': 'a', 'InstalledVersion': '1',
                'Severity': 'HIGH', 'FixedVersion': '2', 'PrimaryURL': 'https://example.invalid/risk'}]
            raw = json.dumps(report).encode('utf-8')
            current = decode_identity(kwargs['source_port'].lookup('resolved').metadata['identity'])
            service = SecurityReviewService(source_port=kwargs['source_port'], scanner_ports={'trivy': adapter})
            result = service.inspect_scan_v2('trivy', raw, plan=kwargs['plan'], current_identity=current,
                exit_code=10, resolved_inventory_ref='resolved', scan_evidence_ref='scan',
                database_evidence_refs=('db',))
            self.assertEqual(result['scan']['findings'][0]['advisory_id'], 'GHSA-existing-risk')
            self.assertEqual(result['scan']['status'], 'INCONCLUSIVE')
            self.assertEqual(result['database_currentness']['status'], 'UNKNOWN')
            self.assertEqual(result['final_gate'], 'NOT_EVALUATED')
            self.assertIn('DATABASE_POLICY_UNAVAILABLE', result['scan']['coverage_gaps'])

    def test_independent_occurrences_not_package_counts_determine_coverage(self):
        from aek.core.security import scanners
        result = scanners.reconcile_occurrences((package(), package("x", "x@1")),
            [occurrence(), occurrence("b", instance="I-b", package_id="b@1")])
        self.assertEqual(result["status"], "INCONCLUSIVE")
        self.assertEqual(result["matched_instance_ids"], ["I-a"])
        self.assertEqual(result["missing_instance_ids"], ["I-b"])
        self.assertEqual(result["unexpected_packages"], 1)

    def test_database_currentness_uses_independent_age_policy_not_next_update(self):
        from aek.core.security import scanners
        policy = scanners.ScanEvidencePolicy("policy-current", 7, (("vulnerability", "registry-approved", 2),))
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        facts = {"kind": "vulnerability", "source": "registry-approved", "schema": 2,
                 "updated_at": "2026-10-10T00:00:00+00:00", "next_update": "2099-01-01T00:00:00Z"}
        result = scanners.database_currentness(facts, policy=policy, now=now)
        self.assertEqual(result["status"], "STALE")
        facts["updated_at"] = "2026-10-18T00:00:00+00:00"
        self.assertEqual(scanners.database_currentness(facts, policy=policy, now=now)["status"], "CURRENT")
        self.assertEqual(scanners.database_currentness(facts, policy=policy, now=now + timedelta(days=8))["status"], "STALE")

    def test_v2_missing_independent_sources_preserves_findings_not_clean(self):
        from aek.adapters.security_scanner import ScannerAdapter
        from aek.core.security.scanners import ScanEvidencePolicy
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = [datetime(2026, 10, 19, tzinfo=timezone.utc)]
            port, _, identity, _ = sources(root, now)
            adapter = ScannerAdapter(root, "trivy")
            raw = json.dumps({"SchemaVersion": 2, "Results": [{"Target": "pom.xml", "Type": "jar",
                "Packages": [{"ID": "a@1", "Name": "a", "Version": "1"}], "Vulnerabilities": [{
                    "VulnerabilityID": "GHSA-example", "PkgName": "a", "InstalledVersion": "1",
                    "Severity": "HIGH", "FixedVersion": "2", "PrimaryURL": "https://example.invalid/advisory"}]}]}).encode()
            result = adapter.normalize_v2(raw, exit_code=10, version="0.75.0", scope_paths=("pom.xml",),
                source_port=port, resolved_inventory_ref="absent", scan_evidence_ref="absent-scan",
                database_evidence_refs=(), identity_digest=identity, scope_ref="a" * 64,
                policy=ScanEvidencePolicy("policy", 7, ()), clock=lambda: now[0])
            self.assertEqual(result.scan.status, "INCONCLUSIVE")
            self.assertEqual(result.scan.findings[0]["advisory_id"], "GHSA-example")
            self.assertEqual(result.scan.findings[0]["fixed_versions"], ["2"])
            self.assertEqual(result.execution["status"], "UNVERIFIED")
            self.assertEqual(result.final_gate, "NOT_EVALUATED")

    def test_v2_complete_current_original_sources_and_actual_files(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, raw, kwargs, _, _, _ = complete_fixture(Path(directory))
            result = adapter.normalize_v2(raw, **kwargs)
            self.assertEqual(result.scan.status, "PASSED", result)
            self.assertEqual(result.execution["status"], "VERIFIED")
            self.assertEqual(result.resolved_coverage["status"], "COMPLETE")
            self.assertEqual(result.scanner_coverage["matched_instance_ids"], ["I-a"])
            self.assertEqual(result.database_currentness["status"], "CURRENT")
            self.assertEqual(result.scan.source_status, "DECLARED")
            self.assertEqual(result.final_gate, "NOT_EVALUATED")

    def test_v2_trusted_empty_inventory_distinct_from_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, raw, kwargs, _, _, _ = complete_fixture(Path(directory), occurrences=[], scanned=[])
            result = adapter.normalize_v2(raw, **kwargs)
            self.assertEqual(result.scan.status, "PASSED", result)
            self.assertEqual(result.resolved_coverage["instance_count"], 0)
            self.assertNotIn("EMPTY_DEPENDENCY_INVENTORY", result.scan.coverage_gaps)

    def test_v2_clock_rechecked_after_material_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, raw, kwargs, now, _, _ = complete_fixture(Path(directory))
            calls = []
            def advancing_clock():
                calls.append(1)
                # At first check the DB is within age, but not when published.
                return now[0] + timedelta(days=0 if len(calls) == 1 else 7)
            kwargs["clock"] = advancing_clock
            result = adapter.normalize_v2(raw, **kwargs)
            self.assertNotEqual(result.scan.status, "PASSED", result)
            self.assertNotEqual(result.database_currentness["status"], "CURRENT")

    def test_duplicate_coordinates_cannot_borrow_scan_across_modules_or_scopes(self):
        from aek.core.security.scanners import reconcile_occurrences
        for variant in ("module", "scope", "resolution_context", "parent_path"):
            first = occurrence()
            second = occurrence(instance="I-b")
            second[variant] = {"module": "other", "scope": "test", "resolution_context": "d" * 64,
                               "parent_path": ["parent"]}[variant]
            with self.subTest(variant=variant):
                result = reconcile_occurrences((package(),), [first, second])
                self.assertEqual(result["status"], "INCONCLUSIVE")
                self.assertEqual(result["missing_instance_ids"], ["I-a", "I-b"])
        self.assertEqual(reconcile_occurrences((package(), package()), [occurrence()])["status"], "INCONCLUSIVE")

    def test_database_missing_timezone_future_schema_or_origin_never_current(self):
        from aek.core.security.scanners import database_currentness, ScanEvidencePolicy
        policy = ScanEvidencePolicy("policy-current", 7, (("vulnerability", "registry-approved", 2),))
        baseline = {"kind": "vulnerability", "source": "registry-approved", "schema": 2,
                    "updated_at": "2026-10-18T00:00:00Z"}
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        for changes in ({"updated_at": "2026-10-18T00:00:00"}, {"updated_at": "2027-01-01T00:00:00Z"},
                        {"updated_at": None}, {"schema": True}, {"schema": 999}, {"source": "untrusted"},
                        {"updated_at": "not a timestamp"}):
            with self.subTest(changes=changes):
                self.assertEqual(database_currentness({**baseline, **changes}, policy=policy, now=now)["status"], "UNKNOWN")

    def test_real_current_database_bytes_and_metadata_cannot_reuse_old_source(self):
        for relative in ("cache/db/trivy.db", "cache/db/metadata.json"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                adapter, raw, kwargs, _, _, _ = complete_fixture(root)
                (root / relative).write_bytes(b"replaced current bytes")
                result = adapter.normalize_v2(raw, **kwargs)
                self.assertEqual(result.scan.status, "INCONCLUSIVE")
                self.assertNotEqual(result.database_currentness["status"], "CURRENT")

    def test_java_database_only_when_actual_identification_path_requires_it(self):
        for required in (False, True):
            with self.subTest(required=required), tempfile.TemporaryDirectory() as directory:
                adapter, raw, kwargs, _, _, _ = complete_fixture(Path(directory), requires_java_db=required)
                result = adapter.normalize_v2(raw, **kwargs)
                self.assertEqual(result.scan.status, "INCONCLUSIVE" if required else "PASSED")

    def test_unmatched_instances_and_unknown_scope_do_not_claim_complete(self):
        for items in ([occurrence(), occurrence("b", instance="I-b", package_id="b@1")],
                      [{**occurrence(), "scanner_ref": None}]):
            with tempfile.TemporaryDirectory() as directory:
                adapter, raw, kwargs, _, _, _ = complete_fixture(Path(directory), occurrences=items)
                result = adapter.normalize_v2(raw, **kwargs)
                self.assertEqual(result.scan.status, "INCONCLUSIVE")
                self.assertEqual(result.scanner_coverage["status"], "INCONCLUSIVE")
                self.assertEqual(result.database_currentness["status"], "CURRENT")
                self.assertNotIn("DATABASE_EVIDENCE_MISSING", result.scan.coverage_gaps)

    def test_revoked_source_after_other_evidence_io_fails_common_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, raw, kwargs, _, _, host = complete_fixture(Path(directory))
            original = kwargs["source_port"]
            class RevokingPort:
                def lookup(self, ref):
                    observation = original.lookup(ref)
                    if ref == "db":
                        host.history.clear()
                    return observation
            kwargs["source_port"] = RevokingPort()
            result = adapter.normalize_v2(raw, **kwargs)
            self.assertEqual(result.scan.status, "INCONCLUSIVE")
            self.assertEqual(result.execution["status"], "UNVERIFIED")
            self.assertIn("SCAN_EVIDENCE_CHANGED_DURING_VALIDATION", result.scan.coverage_gaps)

    def test_wrong_identity_scope_exit_and_raw_digest_cannot_be_borrowed(self):
        for changes in ({"identity_digest": "b" * 64}, {"scope_ref": "other-scope"}, {"exit_code": 10}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                adapter, raw, kwargs, _, _, _ = complete_fixture(Path(directory))
                result = adapter.normalize_v2(raw, **{**kwargs, **changes})
                self.assertNotEqual(result.scan.status, "PASSED")
                self.assertEqual(result.execution["status"], "UNVERIFIED")

    def test_nonempty_inventory_does_not_erase_unscanned_target_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, raw, kwargs, _, register, _ = complete_fixture(root)
            (root / "second-pom.xml").write_text("<project/>", encoding="utf-8")
            plan = adapter.plan(("pom.xml", "second-pom.xml"), identity_digest=kwargs["identity_digest"],
                canonical_scope_ref="a" * 64, configuration_path="config.json",
                cache_files=("cache/db/trivy.db", "cache/db/metadata.json"), required_boundaries=())
            register("scan-new-scope", "scanner-run", {"schema_version": 2, "tool_id": "trivy", "tool_version": "0.75.0",
                "plan_digest": plan.plan_digest, "output_digest": hashlib.sha256(raw).hexdigest(), "exit_code": 0,
                "resolved_inventory_ref": "resolved", "database_evidence_refs": ["db"], "requires_java_db": False})
            kwargs.update(plan=plan, scope_paths=("pom.xml", "second-pom.xml"), scan_evidence_ref="scan-new-scope")
            result = adapter.normalize_v2(raw, **kwargs)
            self.assertEqual(result.scan.status, "INCONCLUSIVE")
            self.assertIn("SCOPE_COVERAGE_INCOMPLETE", result.scan.coverage_gaps)

    def test_database_update_cannot_postdate_its_trusted_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, raw, kwargs, _, register, _ = complete_fixture(Path(directory))
            original = kwargs["source_port"].lookup("db").facts
            register("db-inconsistent-time", "database", original, checked_at="2026-10-17T00:00:00Z")
            scan_facts = kwargs["source_port"].lookup("scan").facts
            register("scan-inconsistent-time", "scanner-run", {**scan_facts,
                "database_evidence_refs": ["db-inconsistent-time"]})
            kwargs.update(database_evidence_refs=("db-inconsistent-time",), scan_evidence_ref="scan-inconsistent-time")
            result = adapter.normalize_v2(raw, **kwargs)
            self.assertEqual(result.scan.status, "INCONCLUSIVE")
            self.assertNotEqual(result.database_currentness["status"], "CURRENT")

    def test_source_expiry_at_final_clock_not_hidden_by_earlier_source_check(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, raw, kwargs, now, _, _ = complete_fixture(Path(directory))
            calls = []
            def expiring_clock():
                calls.append(1)
                if len(calls) > 1:
                    now[0] += timedelta(hours=2)
                return now[0]
            kwargs["clock"] = expiring_clock
            result = adapter.normalize_v2(raw, **kwargs)
            self.assertEqual(result.scan.status, "INCONCLUSIVE")
            self.assertEqual(result.execution["status"], "UNVERIFIED")

    def test_v2_normalization_cannot_expand_the_original_plan_budgets(self):
        for tighter in ({"max_items": 1}, {"max_output_bytes": 100}):
            with self.subTest(tighter=tighter), tempfile.TemporaryDirectory() as directory:
                adapter, raw, kwargs, _, register, _ = complete_fixture(Path(directory),
                    occurrences=[occurrence(), occurrence("b", instance="I-b", package_id="b@1")],
                    scanned=[{"ID": "a@1", "Name": "a", "Version": "1"},
                             {"ID": "b@1", "Name": "b", "Version": "1"}])
                plan = adapter.plan(("pom.xml",), identity_digest=kwargs["identity_digest"],
                    canonical_scope_ref="a" * 64, configuration_path="config.json",
                    cache_files=("cache/db/trivy.db", "cache/db/metadata.json"), required_boundaries=(), **tighter)
                original = kwargs["source_port"].lookup("scan").facts
                register("scan-small-budget", "scanner-run", {**original, "plan_digest": plan.plan_digest})
                kwargs.update(plan=plan, scan_evidence_ref="scan-small-budget")
                result = adapter.normalize_v2(raw, **kwargs)
                self.assertEqual(result.scan.status, "ERROR", result)
                expected = "ITEM_LIMIT_EXCEEDED" if "max_items" in tighter else "OUTPUT_LIMIT_EXCEEDED"
                self.assertIn(expected, result.scan.reason_codes)

    def test_v2_budget_intersection_does_not_coerce_invalid_or_relax_caller_limit(self):
        from dataclasses import replace
        from aek.core.security.scanners import plan_digest
        with tempfile.TemporaryDirectory() as directory:
            adapter, raw, kwargs, _, _, _ = complete_fixture(Path(directory))
            for field, ceiling in (("max_items", 10000), ("max_output_bytes", 2000000)):
                for value in (True, False, 0, -1, None, "1", 1.0, ceiling + 1):
                    with self.subTest(field=field, value=value):
                        result = adapter.normalize_v2(raw, **kwargs, **{field: value})
                        self.assertEqual(result.scan.status, "ERROR")
                        self.assertIn("INVALID_SCANNER_INPUT", result.scan.reason_codes)
                        changed = replace(kwargs["plan"], **{field: value})
                        changed = replace(changed, plan_digest=plan_digest(changed))
                        result = adapter.normalize_v2(raw, **{**kwargs, "plan": changed})
                        self.assertEqual(result.scan.status, "ERROR")
                        self.assertIn("INVALID_SCANNER_INPUT", result.scan.reason_codes)
            result = adapter.normalize_v2(raw, **kwargs, max_output_bytes=1)
            self.assertEqual(result.scan.status, "ERROR")
            self.assertIn("OUTPUT_LIMIT_EXCEEDED", result.scan.reason_codes)


if __name__ == "__main__":
    unittest.main()
