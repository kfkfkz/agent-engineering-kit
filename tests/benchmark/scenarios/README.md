# Public synthetic engineering scenarios

These resources validate the benchmark itself. They are public fixtures, not hidden tests or
evidence that AEK improves real Agent delivery. Actual experiments require the M2/M6 isolation,
source identity, treatment, model/budget and telemetry conditions.

| ID | Contract | Known-bad baseline | Independent checks |
| --- | --- | --- | --- |
| SC-001 | Bounded pagination | Off-by-one endpoint | Full/partial page, zero/empty/out-of-range, preserved JSON values |
| SC-002 | Batch CRUD | Feature not implemented | Create/read/update/delete, invalid/missing input, immutable prior responses |
| SC-003 | Concurrent reservation | Check before synchronization | One success per key, simultaneous duplicates, distinct and empty batches |
| SC-004 | SQLite compatible migration | Drop/recreate legacy table | Historical rows/columns/constraints, old inserts, repeat execution and transactional rollback |
| SC-005 | Shared historical decision | Normalize an opaque external key | Identical decision document for both variants; key compatibility, not keyword citations |
| SC-006 | External commit recovery | Blindly repeat non-idempotent POST | Fresh worker processes, independent loopback service ledger, stable opaque receipt and no duplicate commit |

All six public resource cases are implemented. SC-004 is synthetic SQLite only, not evidence about
other database dialects or production performance. SC-006 simulates an interrupted response after
commit and restarts the worker process; the independent verifier's ledger is authoritative, not a
candidate-authored counter. It needs an ephemeral loopback socket, never a production endpoint.

`catalog.materialize_scenario` selects a known ID and builds a deterministic Git fixture and strict
JSON definition under an owned temporary directory. The JSON pins the selected commit, file digest
and complete verifier bytes. Both paired workspaces get the same fixture; checks and known-good
references are not copied into candidate workspaces. Generated commits/digests are stable for the
same published resource and Git profile, not a substitute for pinning the AEK source revision.

`expected/good` validates the verifier. It is not the source of expected values at runtime: those
values are explicit in `checks/assertions.inc`. Template fragments are concatenated with the bounded
`verify_protocol.py`, compiled and pinned as one standalone Python check; no unchecked helper import
is needed by the verifier. Candidate code runs as a separate subprocess, not inside the verifier's
Python interpreter. Assertion/protocol failures use exit 1; probe infrastructure/budget failures use
exit 2. Raw candidate output is bounded and is not included in result objects.

This is development containment only. It does not establish adversarial file/network isolation or
native Windows process-tree guarantees; those remain release requirements. A forged `PASS` message
and a correct-looking document cannot replace observable behavior.

SC-005 has separate basic outcome and historical-behavior checks. Passing the latter does not prove
the Agent retrieved Memory/MCP; actual retrieval remains a Process metric requiring event evidence.
Socket-disabled execution reports infrastructure failure for SC-006 rather than silently skipping it.
