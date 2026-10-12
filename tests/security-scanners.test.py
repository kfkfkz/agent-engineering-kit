"""Own sanitized protocol projections; no scanner installation or live scan."""
import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def grant_for(plan, *, revoke_after_validation=False):
    from datetime import datetime, timedelta, timezone
    from aek.adapters.security_authority import BuiltinWorkflowAuthority, HostEventSource, runtime_binding
    from aek.core.context.identity import build_identity
    from aek.core.security.authority import HostEvent, digest
    identity = build_identity({key: {"scope_digest": "a" * 64} for key in
                               ("subject", "artifact", "policy", "context", "evidence")})
    class LiveSource(HostEventSource):
        def __init__(self):
            self.events = {}
            self.history = set()
        def consume(self, kind, token):
            if token not in self.events:
                raise ValueError("INVALID_LIVE_EVENT")
            event = self.events.pop(token)
            if event.kind != kind:
                raise ValueError("INVALID_LIVE_EVENT")
            self.history.add(event)
            return event
        def restore(self, event):
            found = event in self.history
            if found and revoke_after_validation:
                self.history.remove(event)
            return found
        def capabilities(self):
            return {"available": True, "mode": "controlled-test-live-events"}
    source = LiveSource()
    authority = BuiltinWorkflowAuthority(repository_ref=plan.repository_ref, task_id="scanner-test",
        scope_ref=plan.scope_ref, current_identity=identity, author="author", host_source=source)
    binding = runtime_binding(**{key: getattr(plan, key) for key in (
        "plan_digest", "repository_ref", "scope_ref", "identity_digest", "tool_id",
        "executable_digest", "action", "network_mode", "write_scope", "timeout_seconds",
        "max_output_bytes", "max_items")})
    now = datetime.now(timezone.utc)
    token = object()
    source.events[token] = HostEvent("scanner-direct-user", "user-confirmation", "human",
        "scanner-test", plan.repository_ref, plan.scope_ref, identity.envelope_digest,
        "execute", digest(binding), now.isoformat(), (now + timedelta(minutes=10)).isoformat())
    return authority, authority.on_user_confirmation(token, binding=binding)


def test_identity_digest():
    from aek.core.context.identity import build_identity
    return build_identity({key: {"scope_digest": "a" * 64} for key in
                         ("subject", "artifact", "policy", "context", "evidence")}).envelope_digest


class ScannerTests(unittest.TestCase):
    def test_control_scope_and_input_snapshot_are_bound_separately(self):
        from aek.adapters.security_scanner import ScannerAdapter
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            (root / "trivy.json").write_text("{}", encoding="utf-8")
            adapter = ScannerAdapter(root, "trivy", (str(Path(sys.executable).resolve()),))
            first = adapter.plan(("app.py",), identity_digest="1" * 64, configuration_path="trivy.json", canonical_scope_ref="a" * 64)
            self.assertEqual(first.scope_ref, "a" * 64)
            (root / "app.py").write_text("value = 2\n", encoding="utf-8")
            second = adapter.plan(("app.py",), identity_digest="1" * 64, configuration_path="trivy.json", canonical_scope_ref="a" * 64)
            self.assertEqual(first.scope_ref, second.scope_ref)
            self.assertNotEqual(first.input_files, second.input_files)
            self.assertNotEqual(first.plan_digest, second.plan_digest)

    def test_trivy_reports_dependency_without_claiming_source_authority(self):
        from aek.core.security.scanners import normalize_scan
        raw = b'{"SchemaVersion":2,"Results":[{"Target":"package-lock.json","Class":"lang-pkgs","Type":"npm","Packages":[{"Name":"example","Version":"1.0"}],"Vulnerabilities":[{"VulnerabilityID":"CVE-2026-0001","PkgName":"example","InstalledVersion":"1.0","Severity":"HIGH"}]}]}'
        result = normalize_scan("trivy", raw, exit_code=10, version="0.75.0",
                                scope_paths=("package-lock.json",), database_digest="a" * 64)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.findings[0]["advisory_id"], "CVE-2026-0001")
        self.assertEqual(result.findings[0]["verification_status"], "SUSPECTED")
        self.assertEqual(result.packages[0]["package"], "example")

    def test_semgrep_partial_scan_is_not_clean(self):
        from aek.core.security.scanners import normalize_scan
        raw = b'{"version":"1.180.0","results":[],"errors":[],"paths":{"scanned":["app.py"],"skipped":[{"path":"other.py","reason":"ignored"}]},"skipped_rules":[]}'
        result = normalize_scan("semgrep", raw, exit_code=0, version="1.180.0",
                                scope_paths=("app.py", "other.py"))
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertIn("SCANNER_SKIPPED_PATHS", result.coverage_gaps)

    def test_gitleaks_secret_and_match_are_not_in_normalized_metadata(self):
        import dataclasses
        import json
        from aek.core.security.scanners import normalize_scan
        raw = b'[{"RuleID":"synthetic-key","File":"app.py","StartLine":1,"Secret":"SIMULATED_SECRET_VALUE","Match":"key=SIMULATED_SECRET_VALUE","Description":"SIMULATED_SECRET_VALUE"}]'
        result = normalize_scan("gitleaks", raw, exit_code=10, version="8.30.1", scope_paths=("app.py",))
        self.assertEqual(result.status, "FAILED")
        self.assertNotIn("SIMULATED_SECRET_VALUE", json.dumps(dataclasses.asdict(result)))
        self.assertNotIn("Match", json.dumps(dataclasses.asdict(result)))

    def test_default_probe_does_not_start_binary_and_version_stays_unknown(self):
        import tempfile
        from aek.adapters.security_scanner import ScannerAdapter
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "tool"
            executable.write_bytes(b"this file must not be executed")
            adapter = ScannerAdapter(Path(directory), "trivy", (str(executable),))
            result = adapter.probe()
            self.assertEqual(result.version, "UNKNOWN")
            self.assertEqual(result.installed, True)
            self.assertEqual(result.version_compatible, "UNKNOWN")
            self.assertEqual(result.authorized, "UNKNOWN")

    def test_plan_is_fixed_and_binds_python_prefix_and_script(self):
        import tempfile
        from aek.adapters.security_scanner import ScannerAdapter
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("print(1)")
            (root / "rules.yml").write_text("rules: []\n")
            (root / "stub.py").write_text("print('1.180.0')")
            python = str(Path(sys.executable).resolve())
            adapter = ScannerAdapter(root, "semgrep", (python, str(root / "stub.py")))
            plan = adapter.plan(("app.py",), identity_digest="a" * 64, configuration_path="rules.yml")
            self.assertEqual(plan.tool_version, "UNKNOWN")
            self.assertIn("--disable-nosem", plan.argv)
            self.assertIn("--metrics", plan.argv)
            self.assertEqual(plan.argv[:2], (python, str(root / "stub.py")))
            (root / "stub.py").write_text("print('changed')")
            changed = adapter.plan(("app.py",), identity_digest="a" * 64, configuration_path="rules.yml")
            self.assertNotEqual(plan.executable_digest, changed.executable_digest)
            self.assertNotEqual(plan.plan_digest, changed.plan_digest)

    def test_real_process_timeout_and_output_budget(self):
        import tempfile
        from aek.adapters.security_process import run_process
        python = str(Path(sys.executable).resolve())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timeout = run_process((python, "-c", "import time; time.sleep(30)"), root,
                                  timeout_seconds=0.1, max_output_bytes=100)
            self.assertEqual(timeout.reason_code, "PROCESS_TIMEOUT")
            self.assertIsNotNone(timeout.exit_code)
            limit = run_process((python, "-c", "print('x'*10000)"), root,
                                timeout_seconds=2, max_output_bytes=100)
            self.assertEqual(limit.reason_code, "OUTPUT_LIMIT_EXCEEDED")
            self.assertLessEqual(len(limit.stdout) + len(limit.stderr), 100)

    def test_authorized_real_version_probe_and_changed_binary_rejection(self):
        import tempfile
        from aek.adapters.security_scanner import ScannerAdapter
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "stub.py"
            script.write_text("print('1.180.0')")
            adapter = ScannerAdapter(root, "semgrep", (str(Path(sys.executable).resolve()), str(script)))
            plan = adapter.plan(identity_digest=test_identity_digest(), canonical_scope_ref="a" * 64, action="version_probe", required_boundaries=())
            authority, grant = grant_for(plan)
            capability = adapter.version_probe(plan, grant=grant, authority=authority)
            self.assertEqual(capability.version, "1.180.0")
            self.assertTrue(capability.version_compatible)
            script.write_text("print('1.180.1')")
            rejected = adapter.version_probe(plan, grant=grant, authority=authority)
            self.assertEqual(rejected.reason_codes, ("SCANNER_PLAN_STALE",))

    def test_three_authorized_scanners_execute_fixed_argv_in_authorized_snapshot(self):
        import tempfile
        import json
        from aek.adapters.security_scanner import ScannerAdapter
        fixture = Path(__file__).resolve().parent / "fixtures/security/scanners"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("key='SYNTHETIC_ONLY'\n")
            (root / "private.txt").write_text("must not be copied")
            (root / "rules.yml").write_text("rules: []\n")
            (root / "empty.json").write_text("{}")
            (root / "gitleaks.toml").write_text("title='test'\n")
            (root / "cache/db").mkdir(parents=True)
            (root / "cache/db/trivy.db").write_bytes(b"synthetic-db")
            for tool, config in (("trivy", "empty.json"), ("semgrep", "rules.yml"), ("gitleaks", "gitleaks.toml")):
                adapter = ScannerAdapter(root, tool, (str(Path(sys.executable).resolve()), str(fixture / (tool + "-double.py"))))
                probe_plan = adapter.plan(identity_digest=test_identity_digest(), canonical_scope_ref="a" * 64, action="version_probe", required_boundaries=())
                authority, grant = grant_for(probe_plan)
                self.assertTrue(adapter.version_probe(probe_plan, authority=authority, grant=grant).version_compatible)
                plan = adapter.plan(("app.py",), identity_digest=test_identity_digest(), canonical_scope_ref="a" * 64, configuration_path=config,
                                    cache_files=("cache/db/trivy.db",) if tool == "trivy" else (), required_boundaries=())
                authority, grant = grant_for(plan)
                result = adapter.execute(plan, authority=authority, grant=grant)
                self.assertEqual(result.status, "FAILED", (tool, result))
                self.assertEqual(result.findings[0]["path"], "app.py")
                self.assertNotIn("SIMULATED_SECRET_VALUE", json.dumps(result.findings))

    def test_forged_rehashed_plan_cannot_change_fixed_argv(self):
        import tempfile
        from dataclasses import replace
        from aek.adapters.security_scanner import ScannerAdapter
        from aek.core.security.scanners import plan_digest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stub.py").write_text("print('1.180.0')")
            adapter = ScannerAdapter(root, "semgrep", (str(Path(sys.executable).resolve()), str(root / "stub.py")))
            plan = adapter.plan(identity_digest=test_identity_digest(), canonical_scope_ref="a" * 64, action="version_probe", required_boundaries=())
            forged = replace(plan, argv=plan.executable_prefix + ("--config", "https://invalid.example"))
            forged = replace(forged, plan_digest=plan_digest(forged))
            authority, grant = grant_for(forged)
            result = adapter.version_probe(forged, grant=grant, authority=authority)
            self.assertEqual(result.reason_codes, ("INVALID_SCANNER_PLAN",))

    def test_no_files_empty_db_or_output_overflow_cannot_pass(self):
        from aek.core.security.scanners import normalize_scan
        cases = (
            ("semgrep", b'{"version":"1.180.0","results":[],"errors":[],"paths":{"scanned":[]},"skipped_rules":[]}', "1.180.0", ()),
            ("trivy", b'{"SchemaVersion":2,"Results":[]}', "0.75.0", ("app.py",)),
            ("gitleaks", b'[]', "8.30.1", ()),
        )
        for tool, raw, version, scope in cases:
            with self.subTest(tool=tool):
                self.assertNotEqual(normalize_scan(tool, raw, exit_code=0, version=version,
                                                   scope_paths=scope).status, "PASSED")
        self.assertEqual(normalize_scan("gitleaks", b'[]', exit_code=0, version="8.30.1",
            scope_paths=("app.py",), max_output_bytes=1).status, "ERROR")

    def test_plan_refuses_private_control_files_and_symlink_inputs(self):
        import tempfile
        from aek.adapters.security_scanner import ScannerAdapter
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stub.py").write_text("print('1.180.0')")
            (root / "rules.yml").write_text("rules: []")
            (root / ".trivyignore").write_text("ignore everything")
            adapter = ScannerAdapter(root, "semgrep", (str(Path(sys.executable).resolve()), str(root / "stub.py")))
            with self.assertRaisesRegex(ValueError, "SCANNER_CONTROL_FILE_REFUSED"):
                adapter.plan((".trivyignore",), identity_digest="a" * 64, configuration_path="rules.yml")
            (root / "real.py").write_text("x=1")
            try:
                (root / "link.py").symlink_to(root / "real.py")
            except OSError:
                # Some Windows accounts cannot create a symlink. Record a real
                # limitation instead of claiming this branch was validated.
                print("SYMLINK_CREATION_UNAVAILABLE: symlink branch not exercised")
            else:
                with self.assertRaisesRegex(ValueError, "SCANNER_SYMLINK_REFUSED"):
                    adapter.plan(("link.py",), identity_digest="a" * 64, configuration_path="rules.yml")

    def test_descendant_holding_pipe_is_stopped_and_not_completed(self):
        import os
        import tempfile
        from aek.adapters.security_process import run_process
        python = str(Path(sys.executable).resolve())
        code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(p.pid,flush=True)"
        with tempfile.TemporaryDirectory() as directory:
            result = run_process((python, "-c", code), Path(directory), timeout_seconds=2, max_output_bytes=1000)
            self.assertEqual(result.reason_code, "PROCESS_DESCENDANT_OPEN_PIPE")
            self.assertTrue(result.cleanup_complete)
            pid = int(result.stdout.strip())
            if os.name == "posix":
                proc = Path("/proc") / str(pid) / "stat"
                if proc.exists():
                    self.assertIn(proc.read_text().rsplit(")", 1)[1].strip()[0], {"Z", "X"})
            elif os.name == "nt":
                import subprocess
                listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                         capture_output=True, timeout=5, check=True)
                self.assertNotIn(f'"{pid}"'.encode(), listing.stdout)

    def test_missing_forged_grants_and_unavailable_boundaries_never_launch(self):
        import tempfile
        from aek.adapters.security_scanner import ScannerAdapter
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "started"
            (root / "stub.py").write_text(f"from pathlib import Path; Path({str(marker)!r}).touch(); print('1.180.0')")
            adapter = ScannerAdapter(root, "semgrep", (str(Path(sys.executable).resolve()), str(root / "stub.py")))
            plan = adapter.plan(identity_digest=test_identity_digest(), canonical_scope_ref="a" * 64, action="version_probe")
            self.assertEqual(adapter.version_probe(plan).reason_codes, ("AUTHORIZATION_REQUIRED",))
            authority, grant = grant_for(plan)
            self.assertEqual(adapter.version_probe(plan, authority=authority, grant={"role": "USER", "approved": True}).reason_codes,
                             ("AUTHORIZATION_REQUIRED",))
            self.assertEqual(adapter.version_probe(plan, authority=authority, grant=grant).reason_codes,
                             ("BOUNDARY_UNAVAILABLE",))
            self.assertFalse(marker.exists())

    def test_bad_json_unsupported_exit_and_escape_paths_are_finite_errors(self):
        from aek.core.security.scanners import normalize_scan
        cases = ((b'{', 0), (b'{"SchemaVersion":2,"SchemaVersion":2,"Results":[]}', 0),
                 (b'{"SchemaVersion":2,"Results":[]}', 2),
                 (b'{"SchemaVersion":2,"Results":[{"Target":"../app.py"}]}', 0))
        for raw, code in cases:
            with self.subTest(raw=raw, code=code):
                result = normalize_scan("trivy", raw, exit_code=code, version="0.75.0", scope_paths=("app.py",))
                self.assertEqual(result.status, "ERROR")
                self.assertLess(len(repr(result)), 1000)
        self.assertEqual(normalize_scan("trivy", b'{}', exit_code=0, version="0.75.1", scope_paths=("app.py",)).reason_codes,
                         ("UNSUPPORTED_VERSION",))

    def test_dangerous_configs_and_budget_inflation_are_refused(self):
        import tempfile
        from aek.adapters.security_scanner import ScannerAdapter
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("x=1")
            (root / "rules.yml").write_text("rules:\n  - validators:\n      http: https://invalid.example\n")
            adapter = ScannerAdapter(root, "semgrep", (str(Path(sys.executable).resolve()),))
            with self.assertRaisesRegex(ValueError, "DANGEROUS_SCANNER_CONFIGURATION"):
                adapter.plan(("app.py",), identity_digest="a" * 64, configuration_path="rules.yml")
            for budget in ({"max_items": 10001}, {"max_output_bytes": 2000001}, {"timeout_seconds": 301}, {"timeout_seconds": True}):
                with self.subTest(budget=budget), self.assertRaisesRegex(ValueError, "INVALID_SCANNER_PLAN"):
                    adapter.plan(identity_digest="a" * 64, action="version_probe", **budget)

    def test_scanner_schema_number_is_strict_and_inventory_needs_database_evidence(self):
        from aek.core.security.scanners import normalize_scan
        raw = b'{"SchemaVersion":2.0,"Results":[]}'
        self.assertEqual(normalize_scan("trivy", raw, exit_code=0, version="0.75.0", scope_paths=("app.py",)).status, "ERROR")
        clean = b'{"SchemaVersion":2,"Results":[{"Target":"app.py","Type":"npm","Packages":[{"Name":"example","Version":"1.0"}],"Vulnerabilities":[]}]}'
        result = normalize_scan("trivy", clean, exit_code=0, version="0.75.0", scope_paths=("app.py",))
        self.assertEqual(result.status, "INCONCLUSIVE")
        self.assertIn("DATABASE_EVIDENCE_MISSING", result.coverage_gaps)

    def test_empty_database_is_blocked_before_scanner_starts(self):
        import tempfile
        from aek.adapters.security_scanner import ScannerAdapter
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("x=1")
            (root / "empty.json").write_text("{}")
            (root / "cache/db").mkdir(parents=True)
            (root / "cache/db/trivy.db").write_bytes(b"")
            script = root / "stub.py"
            script.write_text("import sys; print('Version: 0.75.0' if sys.argv[1:] == ['--version'] else '{\"SchemaVersion\":2,\"Results\":[{\"Target\":\"app.py\",\"Type\":\"npm\",\"Packages\":[{\"Name\":\"example\",\"Version\":\"1.0\"}],\"Vulnerabilities\":[]}]}')")
            adapter = ScannerAdapter(root, "trivy", (str(Path(sys.executable).resolve()), str(script)))
            probe = adapter.plan(identity_digest=test_identity_digest(), canonical_scope_ref="a" * 64, action="version_probe", required_boundaries=())
            authority, grant = grant_for(probe)
            self.assertTrue(adapter.version_probe(probe, grant=grant, authority=authority).version_compatible)
            plan = adapter.plan(("app.py",), identity_digest=test_identity_digest(), canonical_scope_ref="a" * 64, configuration_path="empty.json",
                                cache_files=("cache/db/trivy.db",), required_boundaries=())
            authority, grant = grant_for(plan)
            result = adapter.execute(plan, grant=grant, authority=authority)
            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.reason_codes, ("DATABASE_EVIDENCE_MISSING",))

    def test_real_report_file_and_stderr_share_output_budget(self):
        import tempfile
        from aek.adapters.security_process import run_process
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            code = f"from pathlib import Path; import sys; Path({str(output)!r}).write_bytes(b'x'*100); print('y'*100,file=sys.stderr)"
            result = run_process((str(Path(sys.executable).resolve()), "-c", code), root,
                                 timeout_seconds=2, max_output_bytes=150, output_file=output)
            self.assertEqual(result.reason_code, "OUTPUT_LIMIT_EXCEEDED")
            self.assertTrue(result.cleanup_complete)

    def test_real_process_environment_does_not_inherit_secrets_or_proxies(self):
        import os
        import tempfile
        from aek.adapters.security_process import run_process
        saved = {key: os.environ.get(key) for key in ("AEK_TEST_SECRET", "HTTPS_PROXY", "PYTHONPATH")}
        try:
            for key in saved:
                os.environ[key] = "SYNTHETIC_ENVIRONMENT_SECRET"
            with tempfile.TemporaryDirectory() as directory:
                result = run_process((str(Path(sys.executable).resolve()), "-c",
                    "import os; print(any(key in os.environ for key in ('AEK_TEST_SECRET','HTTPS_PROXY','PYTHONPATH')))"),
                    Path(directory), timeout_seconds=2, max_output_bytes=1000)
                self.assertEqual(result.reason_code, "PROCESS_COMPLETED")
                self.assertEqual(result.stdout.strip(), b"False")
                with self.assertRaisesRegex(ValueError, "INVALID_PROCESS_ENVIRONMENT"):
                    run_process((str(Path(sys.executable).resolve()), "-c", "pass"), Path(directory),
                        timeout_seconds=2, environment={"AEK_TEST_SECRET": "SYNTHETIC_ENVIRONMENT_SECRET"})
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_normalization_rejects_bool_budgets_and_fake_database_digest(self):
        from aek.core.security.scanners import normalize_scan
        result = normalize_scan("gitleaks", b'[]', exit_code=0, version="8.30.1",
                                scope_paths=("app.py",), max_items=True)
        self.assertEqual(result.status, "ERROR")
        raw = b'{"SchemaVersion":2,"Results":[{"Target":"app.py","Type":"npm","Packages":[{"Name":"example","Version":"1.0"}]}]}'
        self.assertEqual(normalize_scan("trivy", raw, exit_code=0, version="0.75.0",
            scope_paths=("app.py",), database_digest="trust me").status, "ERROR")

    def test_unconfigured_probe_does_not_claim_tool_is_not_installed(self):
        import tempfile
        from aek.adapters.security_scanner import ScannerAdapter
        with tempfile.TemporaryDirectory() as directory:
            capability = ScannerAdapter(Path(directory), "trivy").probe()
            self.assertFalse(capability.configured)
            self.assertEqual(capability.installed, "UNKNOWN")
            self.assertEqual(capability.runnable, "UNKNOWN")

    def test_malformed_nested_json_is_a_finite_error_without_exception(self):
        from aek.core.security.scanners import normalize_scan
        raw = b'{"version":"1.180.0","results":[{"path":"app.py","check_id":"rule","extra":[]}],"errors":[],"paths":{"scanned":["app.py"]},"skipped_rules":[]}'
        result = normalize_scan("semgrep", raw, exit_code=1, version="1.180.0", scope_paths=("app.py",))
        self.assertEqual(result.reason_codes, ("INVALID_SCANNER_OUTPUT",))
        self.assertEqual(result.status, "ERROR")

    def test_trivy_package_ids_survive_dependency_projection(self):
        from aek.core.security.scanners import normalize_scan
        raw = b'{"SchemaVersion":2,"Results":[{"Target":"app.py","Type":"npm","Packages":[{"ID":"example@1.0","Name":"example","Version":"1.0"}],"Vulnerabilities":[{"VulnerabilityID":"CVE-2026-0001","PkgID":"example@1.0","PkgName":"example","InstalledVersion":"1.0","Severity":"HIGH"}]}]}'
        result = normalize_scan("trivy", raw, exit_code=10, version="0.75.0", scope_paths=("app.py",), database_digest="a" * 64)
        self.assertEqual(result.packages[0]["package_id"], "example@1.0")
        self.assertEqual(result.findings[0]["package_id"], "example@1.0")

    def test_source_revoked_during_snapshot_staging_blocks_launch(self):
        import tempfile
        from aek.adapters.security_scanner import ScannerAdapter
        fixture = Path(__file__).resolve().parent / "fixtures/security/scanners/semgrep-double.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("x=1")
            (root / "rules.yml").write_text("rules: []")
            adapter = ScannerAdapter(root, "semgrep", (str(Path(sys.executable).resolve()), str(fixture)))
            probe = adapter.plan(identity_digest=test_identity_digest(), canonical_scope_ref="a" * 64, action="version_probe", required_boundaries=())
            authority, grant = grant_for(probe)
            self.assertTrue(adapter.version_probe(probe, authority=authority, grant=grant).version_compatible)
            plan = adapter.plan(("app.py",), identity_digest=test_identity_digest(), canonical_scope_ref="a" * 64, configuration_path="rules.yml", required_boundaries=())
            authority, grant = grant_for(plan, revoke_after_validation=True)
            result = adapter.execute(plan, authority=authority, grant=grant)
            self.assertEqual(result.reason_codes, ("SECURITY_AUTHORITY_INVALID",))
            self.assertEqual(result.status, "BLOCKED")

    def test_escaped_toml_keys_cannot_hide_gitleaks_allowlist(self):
        import tempfile
        from aek.adapters.security_scanner import ScannerAdapter
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("x=1")
            (root / "gitleaks.toml").write_text(r'["allow\u006cist"]' + "\nregexes=['.*']\n")
            adapter = ScannerAdapter(root, "gitleaks", (str(Path(sys.executable).resolve()),))
            with self.assertRaisesRegex(ValueError, "DANGEROUS_SCANNER_CONFIGURATION"):
                adapter.plan(("app.py",), identity_digest="a" * 64, configuration_path="gitleaks.toml", required_boundaries=())

    def test_yaml_line_continuation_cannot_hide_validator_key(self):
        import tempfile
        from aek.adapters.security_scanner import ScannerAdapter
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("x=1")
            (root / "rules.yml").write_text('rules:\n  - "validat' + chr(92) + '\n    ors": []\n')
            adapter = ScannerAdapter(root, "semgrep", (str(Path(sys.executable).resolve()),))
            with self.assertRaisesRegex(ValueError, "DANGEROUS_SCANNER_CONFIGURATION"):
                adapter.plan(("app.py",), identity_digest="a" * 64, configuration_path="rules.yml", required_boundaries=())


if __name__ == "__main__":
    unittest.main()
