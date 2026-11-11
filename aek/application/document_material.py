"""Original approval material, verified against live locked storage.

Neither an archive object nor a caller's digest grants approval. The current
original Gate anchors every proof; format revisions never replace that Gate.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

from aek.core.artifact.document_material import compare_material
from aek.core.artifact.plan import MaterialStageBinding, load_binding_bytes, material_stage_binding

MAX_MATERIAL_HISTORY = 64


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class DocumentMaterials:
    def __init__(self, view, archive, context_loader, *, project_source=None, dependency_loader=None,
                 consumption_loader=None, dependency_material_loader=None, gate_validator=None):
        self.view, self.archive, self.context_loader = view, archive, context_loader
        self.project_source = project_source or (lambda stage, name, raw: raw)
        self.dependency_loader = dependency_loader or (lambda stage: {})
        self.consumption_loader = consumption_loader
        self.dependency_material_loader = dependency_material_loader
        self.gate_validator = gate_validator

    def capture_dependencies(self, stage):
        return dict(self.dependency_loader(stage))

    def _dependency_inputs(self, stage, captured=None):
        if captured is not None:
            raw, hashes = captured
            if raw is not None:
                raw = dict(raw)
                if {name: sha(value) for name, value in raw.items()} != hashes:
                    raise ValueError("SOURCE_CHANGED: captured dependency bytes do not match their epoch")
            elif self.consumption_loader is not None:
                raise ValueError("BASELINE_MISSING: consumption has no dependency bytes")
            return raw, dict(hashes)
        if self.dependency_material_loader is None:
            if self.consumption_loader is not None:
                raise ValueError("BASELINE_MISSING: consumption requires captured dependency bytes")
            return None, self.capture_dependencies(stage)
        sources = dict(self.dependency_material_loader(stage))
        return sources, {name: sha(raw) for name, raw in sources.items()}

    def _compare(self, stage, name, before, after):
        return compare_material(self.project_source(stage, name, before), self.project_source(stage, name, after))

    def _sources(self, documents):
        return {name: self.view.read_source(name) for name in documents}

    def _put(self, raw):
        return self.archive.put(raw, expected_sha256=sha(raw))

    def _gate(self, stage):
        raw = self.view.read_gate(stage)
        if raw is None:
            raise ValueError("BASELINE_MISSING: no original Gate")
        gate = json.loads(raw.decode("utf-8"))
        if self.gate_validator is not None:
            self.gate_validator(gate)
        if (type(gate) is not dict or gate.get("status") != "PASS" or gate.get("stage") != stage
                or type(gate.get("doc_hashes")) is not dict or not gate["doc_hashes"]):
            raise ValueError("BASELINE_MISSING: no original PASS")
        closed_raw = self.view.read_closeout() if stage == "计划" else None
        if closed_raw is not None:
            closed = json.loads(closed_raw.decode("utf-8"))
            if (type(closed) is not dict or closed.get("status") != "PASS"
                    or closed.get("plan_gate_sha256") != sha(raw)
                    or type(closed.get("doc_hashes")) is not dict
                    or set(closed["doc_hashes"]) != set(gate["doc_hashes"])):
                raise ValueError("BASELINE_MISSING: invalid plan closeout")
            gate = {**gate, "doc_hashes": closed["doc_hashes"]}
            raw = raw + b"\0" + closed_raw
        return raw, gate

    def _load(self, stage):
        raw = self.view.snapshot().get(stage + ".plan-binding.json")
        return raw, load_binding_bytes(raw) if raw is not None else None

    def _context(self, stage, documents):
        result = dict(self.context_loader(stage))
        result["source_root"] = self.view.source_identity
        result["source_set"] = sorted(documents)
        return result

    def verify(self, stage, *, current=None, _history_limit=None, _captured_dependencies=None):
        use_live_sources = current is None
        dependency_sources, dependencies = self._dependency_inputs(stage, _captured_dependencies)
        consumers = self.view.consumer_digest
        raw, binding = self._load(stage)
        if not isinstance(binding, MaterialStageBinding):
            raise ValueError("BASELINE_MISSING: no material revision")
        gate_raw, gate = self._gate(stage)
        revision = binding.material_revision
        context = self._context(stage, gate["doc_hashes"])
        if binding.gate_digest != sha(gate_raw) or revision["context"] != context:
            raise ValueError("SOURCE_CHANGED: approval root, policy or Gate changed")
        if set(dict(binding.doc_hashes)) != set(gate["doc_hashes"]):
            raise ValueError("SOURCE_CHANGED: source set changed")
        if binding.binding_kind == "material-only" and dict(binding.doc_hashes) != gate["doc_hashes"]:
            raise ValueError("SOURCE_CHANGED: original approval hashes changed")
        cursor = binding
        seen = set()
        limit = MAX_MATERIAL_HISTORY if _history_limit is None else _history_limit
        for _ in range(limit):
            event = cursor.material_revision
            if (cursor.gate_digest != binding.gate_digest or event["context"] != context
                    or event["origin_binding"] != revision["origin_binding"]
                    or event.get("dependency_identity") != revision.get("dependency_identity")):
                raise ValueError("BASELINE_MISSING: material history changed origin")
            previous = event["previous_revision"]
            if previous is None:
                if event["raw_before"] != gate["doc_hashes"] or event["raw_after"] != gate["doc_hashes"]:
                    raise ValueError("BASELINE_MISSING: first baseline was not EXACT")
                break
            if previous in seen:
                raise ValueError("BASELINE_MISSING: cyclic material history")
            seen.add(previous)
            parent = load_binding_bytes(self.archive.get(previous))
            if (not isinstance(parent, MaterialStageBinding)
                    or event["raw_before"] != parent.material_revision["raw_after"]):
                raise ValueError("BASELINE_MISSING: incomplete material history")
            cursor = parent
        else:
            raise ValueError("BASELINE_MISSING: material history limit")
        originals = {name: self.archive.get(digest) for name, digest in gate["doc_hashes"].items()}
        current = self._sources(originals) if current is None else current
        if {name: sha(value) for name, value in current.items()} != revision["raw_after"]:
            # Only the original execution-record projection may exempt raw
            # changes; new formatting still needs an explicit revision.
            after = {name: self.archive.get(digest) for name, digest in revision["raw_after"].items()}
            if any(self.project_source(stage, name, current[name]) != self.project_source(stage, name, after[name])
                   for name in current):
                raise ValueError("SOURCE_CHANGED: material revision is not current")
        for name, original in originals.items():
            proof = self._compare(stage, name, original, current[name])
            if (proof.classification not in {"EXACT", "EQUIVALENT"}
                    or proof.content_sha256 != revision["content_digest"][name]):
                raise ValueError("NOT_EQUIVALENT: material proof cannot be recomputed")
        dependency = revision.get("dependency_identity")
        if dependency is not None:
            if self.consumption_loader is None or self.consumption_loader(
                    stage, current, dependency, dependency_sources) != dependency:
                raise ValueError("SOURCE_CHANGED: consumed approved slices changed")
        if (self._load(stage)[0] != raw or self._gate(stage)[0] != gate_raw
                or self._context(stage, originals) != context
                or self.capture_dependencies(stage) != dependencies or self.view.consumer_digest != consumers
                or (use_live_sources and self._sources(originals) != current)):
            raise ValueError("SOURCE_CHANGED: material changed during verification")
        return binding

    def baseline(self, stage, *, origin_binding=None, _captured_dependencies=None, _captured_sources=None):
        dependency_sources, dependencies = self._dependency_inputs(stage, _captured_dependencies)
        consumers = self.view.consumer_digest
        gate_raw, gate = self._gate(stage)
        sources = self._sources(gate["doc_hashes"]) if _captured_sources is None else dict(_captured_sources)
        hashes = {name: sha(value) for name, value in sources.items()}
        if hashes != gate["doc_hashes"]:
            raise ValueError("BASELINE_MISSING: first baseline requires EXACT current bytes")
        prior_raw, prior = self._load(stage)
        context = self._context(stage, hashes)
        if (isinstance(prior, MaterialStageBinding) and prior.gate_digest == sha(gate_raw)
                and prior.material_revision["context"] == context
                and prior.material_revision["raw_after"] == hashes):
            return self.verify(stage, _captured_dependencies=(dependency_sources, dependencies))
        content = {name: self._compare(stage, name, raw, raw).content_sha256 for name, raw in sources.items()}
        if any(value is None for value in content.values()):
            raise ValueError("BASELINE_MISSING: unsupported approved material")
        dependency = self.consumption_loader(stage, sources, None, dependency_sources) if self.consumption_loader is not None else None
        binding = material_stage_binding(stage, gate_digest=sha(gate_raw),
            doc_hashes=dict(origin_binding.doc_hashes) if origin_binding is not None else hashes,
            prerequisite_fingerprints=dict(origin_binding.prerequisite_fingerprints)
                if origin_binding is not None else gate.get("upstream_fingerprints", {}),
            context=context, origin_binding=origin_binding,
            raw_before=hashes, raw_after=hashes, content_digest=content, dependency_identity=dependency)
        for raw in (*sources.values(), gate_raw, *((prior_raw,) if prior_raw is not None else ())):
            self._put(raw)
        self._publish(stage, binding, sources, context, consumers, gate_raw, dependencies)
        return binding

    def _publish(self, stage, binding, sources, context, consumers, gate_raw, dependencies):
        hashes = {name: sha(raw) for name, raw in sources.items()}
        shared = hashes.keys() & dependencies.keys()
        if any(hashes[name] != dependencies[name] for name in shared):
            raise ValueError("SOURCE_CHANGED: shared document has different source and dependency bytes")
        raw_binding = canonical(asdict(binding))
        self._put(raw_binding)
        if (self._sources(sources) != sources or self._context(stage, sources) != context
                or self._gate(stage)[0] != gate_raw):
            raise ValueError("SOURCE_CHANGED: material changed before publication")
        self.view.commit_material_refresh({stage + ".plan-binding.json": raw_binding},
            expected_consumers=consumers,
            epoch={"stage": stage, "context": context,
                   "source_hashes": hashes,
                   "dependency_hashes": {name: digest for name, digest in dependencies.items() if name not in shared}},
            context_loader=lambda target: self._context(target, sources))

    def refresh(self, stage, *, expected_revision, source_hashes, dependencies=None):
        dependency_sources, captured_dependencies = self._dependency_inputs(stage)
        if dependencies is not None and dependencies != captured_dependencies:
            raise ValueError("SOURCE_CHANGED: requested dependencies are no longer current")
        dependencies = captured_dependencies
        consumers = self.view.consumer_digest
        gate_raw, gate = self._gate(stage)
        prior_raw, prior = self._load(stage)
        if not isinstance(prior, MaterialStageBinding):
            if expected_revision is not None:
                raise ValueError("CAS_CONFLICT: material revision missing")
            current = self._sources(gate["doc_hashes"])
            if {name: sha(value) for name, value in current.items()} != source_hashes:
                raise ValueError("SOURCE_CHANGED: requested raw hashes are not current")
            binding = self.baseline(stage, origin_binding=prior,
                _captured_dependencies=(dependency_sources, dependencies), _captured_sources=current)
            return "REFRESHED", binding
        revision = prior.material_revision
        current = self._sources(gate["doc_hashes"])
        hashes = {name: sha(value) for name, value in current.items()}
        if hashes != source_hashes:
            raise ValueError("SOURCE_CHANGED: requested raw hashes are not current")
        # Verify the persisted revision against its own immutable bytes first;
        # the new source bytes have not yet obtained a material revision.
        previous_sources = {name: self.archive.get(digest) for name, digest in revision["raw_after"].items()}
        self.verify(stage, current=previous_sources, _captured_dependencies=(dependency_sources, dependencies))
        parent_revision = None
        if revision["previous_revision"] is not None:
            parent_revision = load_binding_bytes(self.archive.get(revision["previous_revision"])).binding_digest
        if hashes == revision["raw_after"] and expected_revision in {prior.binding_digest, parent_revision}:
            if (self._sources(current) != current or self._context(stage, current) != revision["context"]
                    or self._gate(stage)[0] != gate_raw or self._load(stage)[0] != prior_raw
                    or self.view.consumer_digest != consumers or self.capture_dependencies(stage) != dependencies):
                raise ValueError("SOURCE_CHANGED: material changed before UNCHANGED result")
            return "UNCHANGED", prior
        if expected_revision != prior.binding_digest:
            raise ValueError("CAS_CONFLICT: material revision changed")
        # Reserve the next history node before publishing. A valid chain at
        # the verification bound must never be replaced with an unreadable one.
        self.verify(stage, current=previous_sources, _history_limit=MAX_MATERIAL_HISTORY - 1,
                    _captured_dependencies=(dependency_sources, dependencies))
        proofs = {name: self._compare(stage, name, previous_sources[name], current[name]) for name in current}
        if any(item.classification not in {"EXACT", "EQUIVALENT"} for item in proofs.values()):
            raise ValueError("NOT_EQUIVALENT: current sources require review")
        context = self._context(stage, hashes)
        origin = revision["origin_binding"]
        original_binding = load_binding_bytes(canonical(origin)) if origin is not None else None
        binding = material_stage_binding(stage, gate_digest=prior.gate_digest,
            doc_hashes=dict(prior.doc_hashes), prerequisite_fingerprints=dict(prior.prerequisite_fingerprints),
            context=context, origin_binding=original_binding, raw_before=revision["raw_after"], raw_after=hashes,
            content_digest={name: proof.content_sha256 for name, proof in proofs.items()},
            previous_revision=sha(prior_raw), dependency_identity=revision.get("dependency_identity"))
        for raw in (*current.values(), prior_raw):
            self._put(raw)
        self._publish(stage, binding, current, context, consumers, gate_raw, dependencies)
        return "REFRESHED", binding
