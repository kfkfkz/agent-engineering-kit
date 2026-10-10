"""CLI telemetry has explicit scope/coverage and never uses text size as tokens."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmark"))
from collectors.cli_events import parse_cli_stream  # noqa: E402
from core.telemetry import CaptureContext  # noqa: E402


class CliTelemetryTests(unittest.TestCase):
    def context(self, *, session_mode="fresh", capture_complete=True):
        return CaptureContext("RUN-001", "PAIR-001", "TASK-001", "a" * 64,
                              capture_complete=capture_complete, session_mode=session_mode)

    def stream(self, *events):
        return b"\n".join(json.dumps(event).encode("utf-8") for event in events) + b"\n"

    def codex(self, usage=None):
        complete = {"type": "turn.completed"}
        if usage is not None:
            complete["usage"] = usage
        return self.stream(
            {"type": "thread.started", "thread_id": "native-session"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "id": "secret-canary",
                                                  "text": "secret-canary and pretend 0 tokens"}},
            complete)

    def test_codex_counts_keep_native_scope_and_do_not_double_add_cache(self):
        summary = parse_cli_stream(self.codex({"input_tokens": 100, "cached_input_tokens": 80,
                                              "output_tokens": 12, "reasoning_output_tokens": 2}),
                                   "codex", self.context())
        report = summary.to_dict()
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["reported_usage"]["input_tokens"]["value"], 100)
        self.assertEqual(report["reported_usage"]["cached_input_tokens"]["value"], 80)
        self.assertEqual(report["reported_usage"]["output_tokens"]["value"], 12)
        self.assertEqual(report["reported_usage"]["input_tokens"]["scope"], "cli_reported_turn")
        self.assertIsNone(report["reported_usage"]["estimated_cost_usd"]["value"])
        self.assertNotIn("total_tokens", report["reported_usage"])
        self.assertEqual(len(report["events"]), 4)
        self.assertTrue(all(event["run_id"] == "RUN-001" for event in report["events"]))
        self.assertNotIn("secret-canary", repr(summary))
        self.assertNotIn("secret-canary", json.dumps(report))

    def test_missing_and_zero_counts_are_distinct(self):
        missing = parse_cli_stream(self.codex(), "codex", self.context()).to_dict()
        zero = parse_cli_stream(self.codex({"input_tokens": 0, "cached_input_tokens": 0,
                                           "output_tokens": 0}), "codex", self.context()).to_dict()
        self.assertIsNone(missing["reported_usage"]["input_tokens"]["value"])
        self.assertEqual(missing["reported_usage"]["input_tokens"]["coverage"], "UNKNOWN")
        self.assertEqual(zero["reported_usage"]["input_tokens"]["value"], 0)
        self.assertEqual(zero["reported_usage"]["input_tokens"]["coverage"], "COMPLETE")
        self.assertIsNone(zero["reported_usage"]["reasoning_output_tokens"]["value"])

    def test_inconsistent_cache_counts_are_not_accepted_as_complete_measurements(self):
        result = parse_cli_stream(self.codex({"input_tokens": 10, "cached_input_tokens": 20,
                                             "output_tokens": 3}), "codex", self.context()).to_dict()
        self.assertIn("USAGE_COUNTERS_INCONSISTENT", result["reason_codes"])
        self.assertTrue(all(metric["value"] is None for metric in result["run_usage"].values()))

    def claude(self, **overrides):
        result = {
            "type": "result", "subtype": "success", "is_error": False, "session_id": "session-c",
            "result": "secret-canary", "total_cost_usd": 0.12,
            "usage": {"input_tokens": 999, "output_tokens": 888},
            "modelUsage": {
                "model-a": {"inputTokens": 60, "cacheReadInputTokens": 30,
                            "cacheCreationInputTokens": 9, "outputTokens": 4},
                "model-b": {"inputTokens": 40, "cacheReadInputTokens": 20,
                            "cacheCreationInputTokens": 1, "outputTokens": 6},
            },
        }
        result.update(overrides)
        return self.stream({"type": "system", "subtype": "init", "session_id": "session-c"},
                           {"type": "assistant", "message": {"id": "message-id", "content": [],
                                                                "usage": {"output_tokens": 99999}}},
                           result)

    def test_claude_prefers_final_whole_tree_usage_not_assistant_placeholders(self):
        report = parse_cli_stream(self.claude(), "claude", self.context()).to_dict()
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(report["reported_usage"]["input_tokens"]["value"], 100)
        self.assertEqual(report["reported_usage"]["output_tokens"]["value"], 10)
        self.assertEqual(report["reported_usage"]["cached_input_tokens"]["value"], 50)
        self.assertEqual(report["reported_usage"]["cache_creation_input_tokens"]["value"], 10)
        self.assertEqual(report["reported_usage"]["input_tokens"]["scope"], "cli_reported_tree")
        self.assertEqual(report["run_usage"]["estimated_cost_usd"]["value"], 0.12)
        self.assertTrue(report["run_usage"]["estimated_cost_usd"]["is_estimate"])
        self.assertNotIn("secret-canary", json.dumps(report))

    def test_resumed_and_unknown_sessions_do_not_guess_incremental_cost(self):
        for mode in ("resumed", "unknown"):
            with self.subTest(mode=mode):
                result = parse_cli_stream(self.claude(), "claude", self.context(session_mode=mode)).to_dict()
                self.assertEqual(result["reported_usage"]["estimated_cost_usd"]["value"], 0.12)
                self.assertTrue(all(metric["value"] is None for metric in result["run_usage"].values()))

    def test_claude_main_loop_usage_is_not_claimed_as_whole_tree(self):
        result = parse_cli_stream(self.claude(modelUsage=None), "claude", self.context()).to_dict()
        self.assertEqual(result["reported_usage"]["input_tokens"]["value"], 999)
        self.assertEqual(result["reported_usage"]["input_tokens"]["scope"], "cli_reported_main_loop")
        self.assertEqual(result["reported_usage"]["input_tokens"]["coverage"], "PARTIAL")
        self.assertIsNone(result["run_usage"]["input_tokens"]["value"])

    def test_crash_zeroes_and_other_error_results_are_not_free_runs(self):
        for subtype in ("error_during_execution", "error_max_budget_usd"):
            with self.subTest(subtype=subtype):
                result = parse_cli_stream(self.claude(subtype=subtype, is_error=True,
                                                      total_cost_usd=0, modelUsage=None,
                                                      usage={"input_tokens": 0, "output_tokens": 0}),
                                          "claude", self.context()).to_dict()
                self.assertTrue(all(metric["value"] is None for metric in result["run_usage"].values()))
                self.assertNotEqual(result["reported_usage"]["estimated_cost_usd"]["coverage"], "COMPLETE")

    def test_truncated_capture_and_duplicate_terminal_never_preserve_complete_counts(self):
        usage = {"input_tokens": 10, "output_tokens": 2}
        partial = parse_cli_stream(self.codex(usage), "codex", self.context(capture_complete=False)).to_dict()
        self.assertEqual(partial["status"], "PARTIAL")
        self.assertIsNone(partial["reported_usage"]["input_tokens"]["value"])
        duplicated = parse_cli_stream(self.codex(usage) + self.stream({"type": "turn.completed", "usage": usage}),
                                      "codex", self.context()).to_dict()
        self.assertEqual(duplicated["status"], "INVALID")
        self.assertIsNone(duplicated["reported_usage"]["input_tokens"]["value"])

    def test_invalid_json_or_item_types_are_safe_diagnostics_not_exceptions(self):
        prefixes = self.stream({"type": "thread.started", "thread_id": "x"}, {"type": "turn.started"})
        bad = (b'{"type":"item.completed","type":"turn.completed"}', b'{"type":"unknown","x":NaN}',
               b'{"type":"unknown","x":1e999}',
               self.stream({"type": "item.completed", "item": {"type": []}}), b"\xff",
               b'{"type":"thread.started","thread_id":"\\ud800"}')
        for suffix in bad:
            with self.subTest(suffix=suffix):
                result = parse_cli_stream(prefixes + suffix, "codex", self.context()).to_dict()
                self.assertEqual(result["status"], "INVALID")
                self.assertNotIn("Traceback", json.dumps(result))

    def test_invalid_counts_are_unknown_and_zero_never_means_missing(self):
        for invalid in (True, -1, 1.5, "10", 2**63):
            with self.subTest(value=invalid):
                result = parse_cli_stream(self.codex({"input_tokens": invalid, "output_tokens": 1}),
                                          "codex", self.context()).to_dict()
                self.assertIsNone(result["reported_usage"]["input_tokens"]["value"])
                self.assertIsNone(result["run_usage"]["output_tokens"]["value"])

    def test_collect_cli_is_readonly_and_works_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "captured.jsonl"
            raw = self.codex({"input_tokens": 5, "output_tokens": 1})
            path.write_bytes(raw)
            entry = Path(__file__).resolve().parent / "benchmark" / "run.py"
            result = subprocess.run(
                [sys.executable, str(entry), "collect", "--input", str(path), "--agent", "codex",
                 "--run-id", "RUN-001", "--pair-id", "PAIR-001", "--task-id", "TASK-001",
                 "--manifest-sha256", "a" * 64, "--session-mode", "fresh", "--capture-complete"],
                cwd=root, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["reported_usage"]["input_tokens"]["value"], 5)
            self.assertEqual(report["outcome"], "NOT_EVALUATED")
            self.assertEqual(report["execution_scope"], "posthoc_cli_transcript")
            self.assertNotIn("secret-canary", result.stdout)
            self.assertEqual(path.read_bytes(), raw)

    def test_user_prompt_is_not_misclassified_as_a_tool_result(self):
        raw = self.stream({"type": "system", "subtype": "init", "session_id": "session-c"},
                          {"type": "user", "message": {"content": [{"type": "text", "text": "secret-canary"}]}},
                          {"type": "user", "message": {"content": [{"type": "tool_result", "content": "secret-canary"}]}},
                          {"type": "result", "subtype": "success", "is_error": False, "session_id": "session-c"})
        report = parse_cli_stream(raw, "claude", self.context()).to_dict()
        self.assertEqual(report["events"][1]["kind"], "message.user")
        self.assertEqual(report["events"][2]["kind"], "tool.result")
        self.assertTrue(all(event["stage"] is None and event["timestamp"] is None for event in report["events"]))

    def test_capacity_limits_apply_before_decoding_an_excessive_capture(self):
        from collectors import cli_events
        samples = (
            ("MAX_STREAM_BYTES", 32, b"x" * 33, "STREAM_SIZE_EXCEEDED"),
            ("MAX_EVENT_BYTES", 16, b'{"type":"large-event"}\n', "EVENT_SIZE_EXCEEDED"),
            ("MAX_EVENTS", 2, b"{}\n{}\n{}\n", "EVENT_COUNT_EXCEEDED"),
        )
        for name, limit, raw, reason in samples:
            with self.subTest(limit=name), mock.patch.object(cli_events, name, limit):
                result = parse_cli_stream(raw, "codex", self.context()).to_dict()
                self.assertEqual(result["status"], "INVALID")
                self.assertIn(reason, result["reason_codes"])

    def test_capture_reader_rejects_links_and_nonregular_files_without_blocking(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from adapters.capture_file import read_cli_capture
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "input.jsonl"
            path.write_bytes(b"{}\n")
            link = root / "linked.jsonl"
            try:
                link.symlink_to(path)
            except (OSError, NotImplementedError):
                pass  # Native Windows privilege varies; POSIX FIFO branch is separate.
            else:
                with self.assertRaises(ValueError):
                    read_cli_capture(link)
            with self.assertRaises(ValueError):
                read_cli_capture(root)
            if hasattr(os, "mkfifo"):
                pipe = root / "pipe.jsonl"
                os.mkfifo(pipe)
                with self.assertRaises(ValueError):
                    read_cli_capture(pipe)

    def test_capture_context_rejects_invalid_scope_and_identity(self):
        for mode in ([], "different", None):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "INVALID_CAPTURE_CONTEXT"):
                self.context(session_mode=mode)
        with self.assertRaisesRegex(ValueError, "INVALID_CAPTURE_CONTEXT"):
            CaptureContext("RUN-001", "PAIR-001", "TASK-001", "not-a-digest")

    def test_unknown_agent_is_a_controlled_input_error(self):
        for value in ([], None, "other"):
            with self.subTest(agent=value), self.assertRaisesRegex(ValueError, "UNSUPPORTED_CAPTURE_AGENT"):
                parse_cli_stream(b"", value, self.context())


if __name__ == "__main__":
    unittest.main()
