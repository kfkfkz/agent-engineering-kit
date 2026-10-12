# Scanner protocol fixtures

These are original, synthetic, sanitized projections and executable doubles.
They reproduce the bounded JSON field shapes and flags in the frozen contract,
not upstream fixture contents, real vulnerability detection, scanner binaries,
rules, or databases. `synthetic-db` and `SIMULATED_SECRET_VALUE` are test data.

Protocol authority is [scanner-contract-lock.json](scanner-contract-lock.json),
verified on 2026-10-10. The upstream fixture hashes below identify the source
fixtures; they are not hashes of these original projections. Fixture source
tags and executable version probes are separate identities.

| Backend | Source tag | Official source fixture SHA256 |
| --- | --- | --- |
| Trivy | v0.75.0 | 0d90436c81bea86e043c2090e44d1733fa6c29b49d0e212b38373395d7d61dd7 |
| Semgrep | v1.180.0 | a622562f3482ee08ff0fb7e0d84d6b9d6e64b4a1398f881c4c0fe42d103c7601 |
| Gitleaks | v8.30.1 | 009af704252257862ee56642a9711189b6f70ec039a3531255d154664968b7ba |

Official source references:

- https://github.com/aquasecurity/trivy/blob/v0.75.0/integration/testdata/bun.json.golden
- https://github.com/semgrep/semgrep/blob/v1.180.0/cli/tests/default/e2e/snapshots/test_output/test_additional_outputs_with_format_flag/--json/result.json.expected
- https://github.com/gitleaks/gitleaks/blob/v8.30.1/testdata/expected/report/json_simple.json

Upstream program licenses recorded by the contract: Trivy Apache-2.0,
Semgrep LGPL-2.1, Gitleaks MIT. No upstream source/rule/database is copied here;
these original test assets use this repository's license. Tool adoption and
individual database/rule licenses remain separate decisions.

## Process boundary validation

The tests launch Python executable doubles with fixed arguments, a temporary
snapshot containing only authorized files, a filtered environment, real
timeouts, output limits (stdout + stderr + report file), and real descendants.
The production executor controls POSIX process groups and Windows Jobs. On
Windows the leader is created suspended, assigned to a kill-on-close Job, then
resumed through documented Toolhelp/ResumeThread APIs; assignment failures
refuse execution. Windows validation requires running this suite on Windows;
a Linux result does not prove Windows behavior.

Windows implementation references (read-only Microsoft documentation checked
2026-10-10): [AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject),
[THREADENTRY32](https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/ns-tlhelp32-threadentry32),
[JOBOBJECT_EXTENDED_LIMIT_INFORMATION](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information).

These controls do not provide a network or filesystem sandbox. POSIX process
groups do not prevent a hostile executable from detaching via setsid. Default
plans require network and filesystem isolation and therefore refuse execution
with the builtin ProcessPort. A trusted host can explicitly approve a plan
without those isolation requirements for controlled tools; that does not
establish an OS boundary. Existing reports remain importable as DECLARED data,
requiring independent source/freshness verification before security decisions.

No live scanner, actual advisory database, external target, model, or network
scan is exercised by this suite. The ordinary normalized Gitleaks metadata
contains hashed rule identity, authorized path and line; raw Secret/Match,
descriptions, stdout, and stderr are not exported or saved to security records.

Local configuration uses a conservative subset rather than a general YAML/TOML
parser (stdlib Python 3.10 support). Unicode escape spellings and semantic
allowlist/validator/remote/extension/autofix controls are rejected, including
escaped TOML keys and YAML escaped line continuations. This can reject otherwise benign rules; it cannot be used to
claim arbitrary scanner configuration is safe. Trivy accepts only an empty
configuration or `{}`; its operational flags are fixed in the adapter.
