from __future__ import annotations

import hashlib
import importlib.util
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('query_fixtures', ROOT / 'tests/ui-observation.test.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class UiQueryTests(unittest.TestCase):
    def inputs(self):
        from aek.adapters.ui_query import QueryScope, QuerySource
        source, candidate, contract, provenance = fixtures.UiObservationTests().inputs()
        candidate['regions'][0]['layout'] = '查询区域：按名称查询任务 ＡＢＣ'
        record = fixtures.observation.build_observation(source, contract, ('QUERY',), candidate,
                                                       provenance=provenance)
        digest = hashlib.sha256(fixtures.observation.canonical_bytes(record)).hexdigest()
        scope = QueryScope(source['scope_digest'], 'CTX-1', 'PAGE-1', 'default', 'UNKNOWN', 'UNKNOWN')
        return scope, QuerySource(scope, digest, 'c' * 64, record)

    def test_actual_private_memory_query_returns_bounded_chinese_text_and_source_references(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        scope, source = self.inputs()
        checked = []
        with ScopedObservationIndex(scope, (source,), revalidate=lambda value: checked.append(value.sha256) or True) as index:
            result = index.query('名称查询', scope=scope)
            self.assertEqual(len(result.hits), 1)
            self.assertIn('按名称查询任务', result.hits[0]['snippet'])
            self.assertEqual(result.hits[0]['observation_sha256'], source.sha256)
            self.assertEqual(result.hits[0]['evidence_id'], 'P1')
            self.assertEqual(result.stale_count, 0)
            self.assertGreaterEqual(len(checked), 2, 'actual source is checked before loading and returning')

    def test_query_text_is_literal_parameterized_and_normalized_not_sql_or_fts(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        scope, source = self.inputs()
        with ScopedObservationIndex(scope, (source,), revalidate=lambda _: True) as index:
            self.assertEqual(len(index.query('abc', scope=scope).hits), 1)
            for query in ("' OR 1=1 --", '%', '*', 'NEAR(查询,任务)', '查詢'):
                with self.subTest(query=query):
                    self.assertEqual(index.query(query, scope=scope).hits, ())

    def test_region_narrowing_rejects_unknown_duplicate_or_nonimmutable_selection_before_sqlite(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        scope, source = self.inputs()
        for regions in ((), ('UNKNOWN',), ('QUERY', 'QUERY'), ['QUERY'], (True,)):
            with self.subTest(regions=regions), patch('aek.adapters.ui_query.sqlite3.connect') as opened:
                with self.assertRaisesRegex(ValueError, 'region narrowing'):
                    ScopedObservationIndex(scope, (replace(source, region_ids=regions),), revalidate=lambda _: True)
                opened.assert_not_called()
        with ScopedObservationIndex(scope, (replace(source, region_ids=('QUERY',)),), revalidate=lambda _: True) as index:
            self.assertEqual(len(index.query('查询', scope=scope).hits), 1)

    def test_scope_cannot_be_widened_before_import_or_during_query(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        scope, source = self.inputs()
        for key in ('scope_key', 'context_key', 'page_id', 'state_id', 'version_key', 'viewport_key'):
            changed = replace(scope, **{key: ('d' * 64 if key == 'scope_key' else 'OTHER')})
            with self.subTest(dimension=key):
                with patch('aek.adapters.ui_query.sqlite3.connect') as opened:
                    with self.assertRaisesRegex(ValueError, 'outside'):
                        ScopedObservationIndex(scope, (replace(source, scope=changed),), revalidate=lambda _: True)
                    opened.assert_not_called()
                with ScopedObservationIndex(scope, (source,), revalidate=lambda _: True) as index:
                    with self.assertRaisesRegex(ValueError, 'widen'):
                        index.query('查询', scope=changed)

    def test_revoked_source_returns_no_stale_body_and_reports_partial_result(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        scope, source = self.inputs()
        current = True
        with ScopedObservationIndex(scope, (source,), revalidate=lambda _: current) as index:
            current = False
            result = index.query('查询', scope=scope)
            self.assertEqual(result.hits, ())
            self.assertEqual(result.stale_count, 1)
            self.assertNotIn('按名称', str(result))

    def test_later_source_revalidation_cannot_leave_previously_revoked_text_in_hits(self):
        from aek.adapters.ui_query import ScopedObservationIndex, QuerySource
        scope, first = self.inputs()
        source, candidate, contract, provenance = fixtures.UiObservationTests().inputs()
        source['evidence_id'] = 'P2'
        candidate['regions'][0]['layout'] = '查询：另一个区域'
        record = fixtures.observation.build_observation(source, contract, ('QUERY',), candidate,
                                                       provenance=provenance)
        digest = hashlib.sha256(fixtures.observation.canonical_bytes(record)).hexdigest()
        second = QuerySource(scope, digest, 'd' * 64, record)
        first, second = sorted((first, second), key=lambda value: value.sha256)
        current = {first.sha256: True, second.sha256: True}
        querying = False
        def revalidate(value):
            if querying and value.sha256 == second.sha256:
                current[first.sha256] = False
            return current[value.sha256]
        with ScopedObservationIndex(scope, (first, second), revalidate=revalidate) as index:
            querying = True
            result = index.query('查询', scope=scope)
            self.assertFalse(current[first.sha256])
            self.assertEqual([row['observation_sha256'] for row in result.hits], [second.sha256])
            self.assertEqual(result.stale_count, 1)

    def test_bad_sources_are_not_imported_even_with_matching_caller_flags(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        scope, source = self.inputs()
        for sources in ((replace(source, sha256='e' * 64),), (source, source), (source,) * 257):
            with self.subTest(count=len(sources)), self.assertRaises(ValueError), patch(
                    'aek.adapters.ui_query.sqlite3.connect') as opened:
                try:
                    ScopedObservationIndex(scope, sources, revalidate=lambda _: True)
                finally:
                    opened.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'currently authorized'):
            ScopedObservationIndex(scope, (source,), revalidate=lambda _: False)

    def test_sqlite_is_private_memory_and_read_only_with_no_external_io_sql(self):
        from aek.adapters.ui_query import ScopedObservationIndex, _SELECT
        scope, source = self.inputs()
        connect = sqlite3.connect
        with patch('aek.adapters.ui_query.sqlite3.connect', wraps=connect) as opened:
            with ScopedObservationIndex(scope, (source,), revalidate=lambda _: True) as index:
                opened.assert_called_once_with(':memory:')
                for statement in ("ATTACH DATABASE '/tmp/unapproved.db' AS other", 'PRAGMA database_list',
                                  'CREATE TABLE evil(x)', 'DELETE FROM ui_rows',
                                  "SELECT load_extension('/tmp/evil')", 'SELECT * FROM sqlite_master'):
                    with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                        index._connection.execute(statement)
                plan = index._connection.execute('EXPLAIN QUERY PLAN ' + _SELECT,
                    (*scope.__dict__.values(), '查询', 20)).fetchall()
                self.assertTrue(any('USING INDEX ui_scope' in str(row) for row in plan), plan)
                self.assertEqual(len(index.query('查询', scope=scope).hits), 1)
            self.assertIsNone(index._connection)

    def test_input_and_result_budgets_do_not_silently_truncate_requests(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        scope, source = self.inputs()
        with ScopedObservationIndex(scope, (source,), revalidate=lambda _: True) as index:
            for text, limit in (('中' * 257, 20), ('', 20), ('\0', 20), ('查询', 21), ('查询', True)):
                with self.subTest(text=text[:20], limit=limit), self.assertRaises(ValueError):
                    index.query(text, scope=scope, limit=limit)
        with patch('aek.adapters.ui_query.MAX_BYTES', 10):
            with self.assertRaisesRegex(ValueError, 'byte limit'):
                ScopedObservationIndex(scope, (source,), revalidate=lambda _: True)

    def test_connection_failure_is_explicit_unavailable_and_does_not_call_models(self):
        from aek.adapters.ui_query import ScopedObservationIndex, QueryUnavailable
        scope, source = self.inputs()
        with patch('aek.adapters.ui_query.sqlite3.connect', side_effect=sqlite3.OperationalError('failure')):
            with self.assertRaisesRegex(QueryUnavailable, 'unavailable'):
                ScopedObservationIndex(scope, (source,), revalidate=lambda _: True)
        for _ in range(2):
            with ScopedObservationIndex(scope, (source,), revalidate=lambda _: True) as index:
                result = index.query('查询', scope=scope)
                self.assertEqual(len(result.hits), 1)

    def test_query_timeout_closes_the_projection_without_returning_body(self):
        from aek.adapters.ui_query import ScopedObservationIndex, QueryUnavailable
        scope, source = self.inputs()
        with ScopedObservationIndex(scope, (source,), revalidate=lambda _: True) as index:
            # Controlled clock at the real adapter boundary; no blocking sleep.
            with patch('aek.adapters.ui_query.time.monotonic', side_effect=[0.0, 3.0]):
                with self.assertRaises(TimeoutError):
                    index.query('查询', scope=scope)
            with self.assertRaises(QueryUnavailable):
                index.query('查询', scope=scope)

    def test_no_hit_query_still_obeys_deadline(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        scope, source = self.inputs()
        with ScopedObservationIndex(scope, (source,), revalidate=lambda _: True) as index:
            with patch('aek.adapters.ui_query.time.monotonic', side_effect=[0.0, 3.0]):
                with self.assertRaises(TimeoutError):
                    index.query('没有命中', scope=scope)

    def test_source_revalidator_cannot_accidentally_mutate_the_private_projection(self):
        from aek.adapters.ui_query import ScopedObservationIndex
        scope, source = self.inputs()
        def inspect_then_mutate(value):
            value.record['regions'][0]['layout'] = 'unhashed replacement text'
            return True
        with ScopedObservationIndex(scope, (source,), revalidate=inspect_then_mutate) as index:
            self.assertEqual(index.query('unhashed', scope=scope).hits, ())
            self.assertEqual(len(index.query('名称查询', scope=scope).hits), 1)

    def test_real_sqlite_result_snippets_and_count_are_bounded(self):
        from aek.adapters.ui_query import ScopedObservationIndex, QuerySource
        scope, source = self.inputs()
        original, candidate, contract, provenance = fixtures.UiObservationTests().inputs()
        candidate['regions'] = [dict(candidate['regions'][0], region_id=f'R{number}',
            layout=('查询' + '中' * 900 if number == 0 else '查询区域')) for number in range(25)]
        required = tuple(region['region_id'] for region in candidate['regions'])
        record = fixtures.observation.build_observation(original, contract, required, candidate,
                                                       provenance=provenance)
        sha = hashlib.sha256(fixtures.observation.canonical_bytes(record)).hexdigest()
        source = QuerySource(scope, sha, source.binding_sha256, record)
        with ScopedObservationIndex(scope, (source,), revalidate=lambda _: True) as index:
            result = index.query('查询', scope=scope)
            self.assertEqual(len(result.hits), 20)
            self.assertTrue(result.truncated)
            self.assertTrue(all(len(row['snippet']) <= 512 for row in result.hits))
            self.assertEqual(len(next(row for row in result.hits if row['region_id'] == 'R0')['snippet']), 512)

    def test_missing_safe_sqlite_api_closes_connection_and_is_not_silently_ignored(self):
        from aek.adapters.ui_query import ScopedObservationIndex, QueryUnavailable
        scope, source = self.inputs()
        actual = sqlite3.connect(':memory:')
        class MissingConfig:
            def __getattr__(self, name):
                if name == 'setconfig':
                    raise AttributeError(name)
                return getattr(actual, name)
        with patch('aek.adapters.ui_query.sqlite3.connect', return_value=MissingConfig()):
            with self.assertRaises(QueryUnavailable):
                ScopedObservationIndex(scope, (source,), revalidate=lambda _: True)
        with self.assertRaises(sqlite3.ProgrammingError):
            actual.execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
