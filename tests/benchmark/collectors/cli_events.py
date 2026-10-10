"""Strict single-shot CLI JSONL decoding; no authentication, execution or raw logs.

Protocol source: https://learn.chatgpt.com/docs/non-interactive-mode
Claude: https://code.claude.com/docs/en/agent-sdk/cost-tracking
Numeric fields retain their native scope. No cache/reasoning counts are added a
second time, no byte-to-token conversion and no inferred billing total.
"""
from __future__ import annotations

import hashlib
import json
import math

from core.telemetry import CaptureContext, Measurement, NormalizedEvent, StreamSummary

MAX_STREAM_BYTES = 16 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
MAX_EVENTS = 10_000
_METRICS = ("input_tokens", "cached_input_tokens", "cache_creation_input_tokens", "output_tokens",
            "reasoning_output_tokens", "estimated_cost_usd")


def _unknown(agent: str) -> dict[str, Measurement]:
    return {key: Measurement(None, f"{agent}.not_observed", key == "estimated_cost_usd",
                             "UNKNOWN", "unknown") for key in _METRICS}


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("DUPLICATE_EVENT_KEY")
        value[key] = item
    return value


def _nonfinite(_value):
    raise ValueError("NONFINITE_EVENT_VALUE")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("NONFINITE_EVENT_VALUE")
    return number


def _count(value) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _claude_usage(event: dict, reasons: list[str]) -> dict[str, Measurement]:
    metrics = _unknown("claude")
    if event["subtype"] == "error_during_execution":
        reasons.append("CRASH_TOTALS_UNRELIABLE")
        return metrics
    models = event.get("modelUsage")
    mapping = {"input_tokens": "inputTokens", "cached_input_tokens": "cacheReadInputTokens",
               "cache_creation_input_tokens": "cacheCreationInputTokens", "output_tokens": "outputTokens"}
    if isinstance(models, dict) and 0 < len(models) <= 64 and all(isinstance(item, dict) for item in models.values()):
        for key, native in mapping.items():
            values = [_count(item.get(native)) for item in models.values()]
            if all(value is not None for value in values) and sum(values) <= 2**63 - 1:
                metrics[key] = Measurement(sum(values), "claude.result.modelUsage", False,
                                           "COMPLETE", "cli_reported_tree")
            elif any(native in item for item in models.values()):
                reasons.append("USAGE_FIELD_INVALID")
    else:
        if models is not None:
            reasons.append("MODEL_USAGE_INVALID")
        usage = event.get("usage")
        if isinstance(usage, dict):
            for key, native in {"input_tokens": "input_tokens", "cached_input_tokens": "cache_read_input_tokens",
                                "cache_creation_input_tokens": "cache_creation_input_tokens",
                                "output_tokens": "output_tokens"}.items():
                value = _count(usage.get(native))
                if value is not None:
                    metrics[key] = Measurement(value, "claude.result.usage", False,
                                               "PARTIAL", "cli_reported_main_loop")
                elif native in usage:
                    reasons.append("USAGE_FIELD_INVALID")
    cost = event.get("total_cost_usd")
    if type(cost) in {int, float} and 0 <= cost <= 1_000_000_000 and math.isfinite(cost):
        metrics["estimated_cost_usd"] = Measurement(float(cost), "claude.result.total_cost_usd", True,
                                                    "COMPLETE", "cli_reported_session_or_call")
    elif cost is not None:
        reasons.append("COST_FIELD_INVALID")
    return metrics


def parse_cli_stream(raw: bytes, agent: str, context: CaptureContext) -> StreamSummary:
    if not isinstance(raw, bytes) or not isinstance(context, CaptureContext):
        raise ValueError("INVALID_CAPTURE_INPUT")
    if not isinstance(agent, str) or agent not in {"codex", "claude"}:
        raise ValueError("UNSUPPORTED_CAPTURE_AGENT")
    metrics = _unknown(agent)
    events = []
    reasons = []
    session = None
    state = "initial"
    stream_digest = hashlib.sha256(raw).hexdigest()

    def summary(status):
        reported = metrics.copy() if status == "COMPLETE" else _unknown(agent)
        if reasons:
            reported = {key: Measurement(metric.value, metric.source, metric.is_estimate,
                                          "PARTIAL" if metric.value is not None else "UNKNOWN", metric.scope)
                        for key, metric in reported.items()}
        run = _unknown(agent)
        if (status == "COMPLETE" and not reasons and state == "completed"
                and context.capture_complete and context.session_mode == "fresh"):
            run = {key: metric if metric.coverage == "COMPLETE" else run[key]
                   for key, metric in reported.items()}
        return StreamSummary(agent, status, tuple(dict.fromkeys(reasons)), stream_digest, session,
                             context.manifest_sha256, tuple(events), tuple(reported.items()), tuple(run.items()),
                             context.session_mode, context.capture_complete)

    if len(raw) > MAX_STREAM_BYTES:
        reasons.append("STREAM_SIZE_EXCEEDED")
        return summary("INVALID")
    pieces = raw.split(b"\n", MAX_EVENTS + 1)
    if len(pieces) > MAX_EVENTS + 1:
        reasons.append("EVENT_COUNT_EXCEEDED")
        return summary("INVALID")
    lines = [line.rstrip(b"\r") for line in pieces if line.strip()]
    if len(lines) > MAX_EVENTS:
        reasons.append("EVENT_COUNT_EXCEEDED")
        return summary("INVALID")
    for sequence, line in enumerate(lines, 1):
        if len(line) > MAX_EVENT_BYTES:
            reasons.append("EVENT_SIZE_EXCEEDED")
            return summary("INVALID")
        try:
            event = json.loads(line.decode("utf-8"), object_pairs_hook=_unique,
                               parse_constant=_nonfinite, parse_float=_finite_float)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("EVENT_SHAPE_INVALID")
        except (ValueError, UnicodeError, RecursionError):
            reasons.append("EVENT_JSON_INVALID")
            return summary("INVALID")
        event_type = event["type"]
        kind, tool = "unknown", None
        if state in {"completed", "failed"}:
            reasons.append("EVENT_AFTER_TERMINAL")
            return summary("INVALID")
        if agent == "claude":
            if event_type == "system" and event.get("subtype") == "init" and state == "initial":
                identifier = event.get("session_id")
                try:
                    if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
                        raise ValueError("session")
                    session = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
                except (ValueError, UnicodeError):
                    reasons.append("NATIVE_SESSION_INVALID")
                    return summary("INVALID")
                state, kind = "active", "run.started"
            elif event_type in {"assistant", "user", "stream_event", "tool_progress", "tool_use_summary",
                                "rate_limit_event", "auth_status"} and state == "active":
                kind = {"assistant": "message", "user": "message.user", "stream_event": "message.delta",
                        "tool_progress": "tool.progress", "tool_use_summary": "tool.summary",
                        "rate_limit_event": "rate_limit", "auth_status": "auth.status"}[event_type]
                if event_type in {"assistant", "user"}:
                    message = event.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if isinstance(content, list):
                        types = {part.get("type") for part in content
                                 if isinstance(part, dict) and isinstance(part.get("type"), str)}
                        if "tool_use" in types and event_type == "assistant":
                            kind = "tool.started"
                        elif "tool_result" in types and event_type == "user":
                            kind = "tool.result"
            elif event_type == "result" and state == "active":
                identifier = event.get("session_id")
                try:
                    if not isinstance(identifier, str) or hashlib.sha256(identifier.encode("utf-8")).hexdigest() != session:
                        raise ValueError("session")
                except (ValueError, UnicodeError):
                    reasons.append("NATIVE_SESSION_MISMATCH")
                    return summary("INVALID")
                subtype, failed = event.get("subtype"), event.get("is_error")
                if not isinstance(subtype, str) or type(failed) is not bool or event.get("parent_tool_use_id"):
                    reasons.append("RESULT_SHAPE_INVALID")
                    return summary("INVALID")
                if (subtype == "success") == failed:
                    reasons.append("RESULT_FLAGS_INCONSISTENT")
                    return summary("INVALID")
                state, kind = ("failed" if failed else "completed"), "run.result"
                metrics = _claude_usage(event, reasons)
                if failed:
                    reasons.append("NATIVE_FAILURE_OBSERVED")
            elif event_type in {"result", "system"}:
                reasons.append("LIFECYCLE_INVALID")
                return summary("INVALID")
            else:
                reasons.append("UNKNOWN_EVENT_TYPE")
        elif event_type == "thread.started" and state == "initial":
            identifier = event.get("thread_id")
            if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
                reasons.append("NATIVE_SESSION_INVALID")
                return summary("INVALID")
            try:
                session = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
            except UnicodeError:
                reasons.append("NATIVE_SESSION_INVALID")
                return summary("INVALID")
            state, kind = "thread", "run.started"
        elif event_type == "turn.started" and state == "thread":
            state, kind = "active", "turn.started"
        elif event_type in {"item.started", "item.updated", "item.completed"} and state == "active":
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                reasons.append("ITEM_SHAPE_INVALID")
                return summary("INVALID")
            kind = event_type
            if item.get("type") in {"command_execution", "mcp_tool_call", "web_search"}:
                tool = item["type"]
        elif event_type == "turn.completed" and state == "active":
            state, kind = "completed", "turn.completed"
            usage = event.get("usage")
            if isinstance(usage, dict):
                for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
                    value = _count(usage.get(key))
                    if value is not None:
                        metrics[key] = Measurement(value, "codex.turn.completed.usage", False,
                                                   "COMPLETE", "cli_reported_turn")
                    elif key in usage:
                        reasons.append("USAGE_FIELD_INVALID")
                for total, subset in (("input_tokens", "cached_input_tokens"),
                                      ("output_tokens", "reasoning_output_tokens")):
                    if (metrics[total].value is not None and metrics[subset].value is not None
                            and metrics[subset].value > metrics[total].value):
                        reasons.append("USAGE_COUNTERS_INCONSISTENT")
            else:
                reasons.append("USAGE_NOT_OBSERVED")
        elif event_type == "turn.failed" and state == "active":
            state, kind = "failed", "turn.failed"
        elif event_type == "error":
            kind = "error"
            reasons.append("NATIVE_ERROR_OBSERVED")
        elif event_type in {"thread.started", "turn.started", "turn.completed", "turn.failed"}:
            reasons.append("LIFECYCLE_INVALID")
            return summary("INVALID")
        else:
            reasons.append("UNKNOWN_EVENT_TYPE")
        digest = hashlib.sha256(line).hexdigest()
        event_id = hashlib.sha256(
            f"{context.manifest_sha256}:{context.run_id}:{sequence}:{digest}".encode("utf-8")).hexdigest()
        events.append(NormalizedEvent(sequence, event_id, context.run_id, context.pair_id,
                                      context.task_id, f"{agent}.jsonl", kind, digest, tool))
    if not lines:
        reasons.append("EMPTY_TRANSCRIPT")
        return summary("NOT_EVALUATED")
    if state not in {"completed", "failed"} or not context.capture_complete:
        reasons.append("CAPTURE_INCOMPLETE")
        return summary("PARTIAL")
    return summary("COMPLETE")
