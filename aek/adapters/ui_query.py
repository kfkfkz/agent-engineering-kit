"""Disposable, single-scope observation text projection, not an authority.

Internal port: the control-side caller must supply its current authorized scope,
adopted runtime and a real source/context revalidator. No JSON/CLI flag grants
these capabilities. Source loading and the public workflow remain separate.
"""
from __future__ import annotations

from dataclasses import astuple, dataclass
import hashlib
import json
import re
import sqlite3
import time
import unicodedata

from aek.core.artifact.ui_observation import canonical_bytes, validate_observation

MAX_SOURCES = 256
MAX_BYTES = 8 * 1024 * 1024
MAX_HITS = 20
MAX_SNIPPET = 512
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_COLUMNS = ('scope_key', 'context_key', 'page_id', 'state_id', 'version_key', 'viewport_key',
            'observation_sha', 'evidence_id', 'region_id', 'image_sha', 'contract_sha',
            'binding_sha', 'observation_kind', 'text_folded', 'text_display')
_SELECT = ("SELECT observation_sha,evidence_id,region_id,image_sha,contract_sha,binding_sha,"
    "observation_kind,text_display FROM ui_rows INDEXED BY ui_scope WHERE scope_key=? "
    "AND context_key=? AND page_id=? AND state_id=? AND version_key=? AND viewport_key=? "
    "AND instr(text_folded,?)>0 ORDER BY observation_sha,region_id LIMIT ?")


class QueryUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class QueryScope:
    scope_key: str
    context_key: str
    page_id: str
    state_id: str
    version_key: str
    viewport_key: str


@dataclass(frozen=True)
class QuerySource:
    scope: QueryScope
    sha256: str
    binding_sha256: str
    record: dict
    region_ids: tuple[str, ...] | None = None


@dataclass(frozen=True)
class QueryResult:
    hits: tuple[dict, ...]
    stale_count: int
    generation: str
    truncated: bool = False


def _fold(value: str) -> str:
    return unicodedata.normalize('NFKC', value).casefold()


def _sha(value):
    if type(value) is not str or _SHA.fullmatch(value) is None:
        raise ValueError('invalid query source digest')


class ScopedObservationIndex:
    def __init__(self, scope: QueryScope, sources: tuple[QuerySource, ...], *, revalidate,
                 timeout: float = 2.0):
        if (type(scope) is not QueryScope or any(type(value) is not str or not value
                or len(value) > 256 or '\0' in value for value in astuple(scope))):
            raise ValueError('invalid query scope')
        _sha(scope.scope_key)
        if type(sources) is not tuple or len(sources) > MAX_SOURCES or not callable(revalidate):
            raise ValueError('invalid query sources or revalidator')
        if type(timeout) not in (int, float) or not 0 < timeout <= 2:
            raise ValueError('invalid query deadline')
        self.scope, self._revalidate, self._timeout = scope, revalidate, timeout
        self._deadline = time.monotonic() + timeout
        self._connection = None
        self._sources = {}
        total, rows = 0, []
        for source in sources:
            self._check_deadline()
            if type(source) is not QuerySource or source.scope != scope:
                raise ValueError('source is outside the control-selected query scope')
            _sha(source.sha256)
            _sha(source.binding_sha256)
            record = validate_observation(source.record)
            raw = canonical_bytes(record)
            total += len(raw)
            if total > MAX_BYTES:
                raise ValueError('query sources exceed byte limit')
            if (hashlib.sha256(raw).hexdigest() != source.sha256
                    or record['evidence_scope']['scope_digest'] != scope.scope_key
                    or source.sha256 in self._sources):
                raise ValueError('query source digest, scope or uniqueness mismatch')
            # A private copy: later caller mutation cannot alter source metadata
            # while leaving stale indexed text behind.
            record = json.loads(raw)
            allowed = source.region_ids
            if allowed is not None and (type(allowed) is not tuple or not allowed
                    or any(type(ident) is not str for ident in allowed)
                    or len(set(allowed)) != len(allowed)
                    or not set(allowed) <= {region['region_id'] for region in record['regions']}):
                raise ValueError('invalid query region narrowing')
            frozen = QuerySource(scope, source.sha256, source.binding_sha256, record, allowed)
            if not self._currently_valid(frozen):
                raise ValueError('query source is not currently authorized and valid')
            self._sources[source.sha256] = frozen
            image = record['evidence_scope']
            contract = hashlib.sha256(canonical_bytes(record['extraction_contract'])).hexdigest()
            for region in record['regions']:
                if allowed is not None and region['region_id'] not in allowed:
                    continue
                display = '\n'.join([region['layout'], *[
                    ' '.join((control['kind'], control['label'], control['state']))
                    for control in region['controls']], *region['style_clues']])
                rows.append((*astuple(scope), source.sha256, image['evidence_id'], region['region_id'],
                    image['image_sha256'], contract, source.binding_sha256, region['observation_kind'],
                    _fold(display), display))
        self.generation = hashlib.sha256(canonical_bytes({'scope': astuple(scope), 'sources': sorted(
            (source.sha256, source.binding_sha256, source.region_ids)
            for source in self._sources.values())})).hexdigest()
        try:
            self._connection = sqlite3.connect(':memory:')
            db = self._connection
            for key, value in (('SQLITE_DBCONFIG_DEFENSIVE', True),
                               ('SQLITE_DBCONFIG_TRUSTED_SCHEMA', False),
                               ('SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION', False)):
                db.setconfig(getattr(sqlite3, key), value)
            for key, value in (('SQLITE_LIMIT_LENGTH', 256 * 1024), ('SQLITE_LIMIT_SQL_LENGTH', 8192),
                               ('SQLITE_LIMIT_VARIABLE_NUMBER', 16), ('SQLITE_LIMIT_ATTACHED', 0),
                               ('SQLITE_LIMIT_COLUMN', 24), ('SQLITE_LIMIT_COMPOUND_SELECT', 0)):
                db.setlimit(getattr(sqlite3, key), value)
            db.execute('PRAGMA temp_store=MEMORY')
            db.set_progress_handler(lambda: int(time.monotonic() >= self._deadline), 100)
            db.execute('CREATE TABLE ui_rows (' + ','.join(name + ' TEXT NOT NULL' for name in _COLUMNS)
                       + ',PRIMARY KEY(scope_key,context_key,observation_sha,region_id))')
            db.execute('CREATE INDEX ui_scope ON ui_rows(scope_key,context_key,page_id,state_id,'
                       'version_key,viewport_key,observation_sha,region_id)')
            db.executemany('INSERT INTO ui_rows VALUES (' + ','.join('?' for _ in _COLUMNS) + ')', rows)
            db.commit()
            db.execute('PRAGMA query_only=ON')
            db.set_authorizer(self._authorize)
            self._check_deadline()
        except (AttributeError, sqlite3.Error) as exc:
            self.close()
            raise QueryUnavailable('safe private-memory SQLite initialization unavailable') from exc
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _authorize(action, arg1, arg2, database, _trigger):
        allowed = (action == sqlite3.SQLITE_SELECT
            or (action == sqlite3.SQLITE_READ and database == 'main' and arg1 == 'ui_rows'
                and arg2 in _COLUMNS)
            or (action == sqlite3.SQLITE_FUNCTION and arg2 == 'instr'))
        return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY

    def _check_deadline(self):
        if time.monotonic() >= self._deadline:
            raise TimeoutError('observation query deadline exceeded')

    def _currently_valid(self, source: QuerySource) -> bool:
        candidate = QuerySource(source.scope, source.sha256, source.binding_sha256,
                                json.loads(canonical_bytes(source.record)), source.region_ids)
        return self._revalidate(candidate) is True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def query(self, text: str, *, scope: QueryScope, limit: int = MAX_HITS) -> QueryResult:
        if self._connection is None:
            raise QueryUnavailable('observation query connection is closed')
        if type(scope) is not QueryScope or scope != self.scope:
            raise ValueError('query cannot widen the control-selected scope')
        if type(text) is not str or not text.strip() or len(text) > 256 or '\0' in text:
            raise ValueError('invalid observation query text')
        if type(limit) is not int or not 1 <= limit <= MAX_HITS:
            raise ValueError('invalid observation query result limit')
        self._deadline = time.monotonic() + self._timeout
        try:
            rows = self._connection.execute(_SELECT, (*astuple(scope), _fold(text), limit + 1)).fetchall()
            self._check_deadline()
            truncated = len(rows) > limit
            rows = rows[:limit]
            hits, current = [], {}
            identities = tuple(dict.fromkeys(row[0] for row in rows))
            # One source check can perform IO during which another source is
            # revoked. Keep candidate text private until the complete pass and
            # a second bounded check of still-current sources have finished.
            for pass_number in range(2):
                for sha in identities:
                    self._check_deadline()
                    if pass_number == 0 or current[sha]:
                        current[sha] = self._currently_valid(self._sources[sha])
                    self._check_deadline()
            for sha, evidence, region, image, contract, binding, kind, display in rows:
                if current[sha]:
                    hits.append({'observation_sha256': sha, 'evidence_id': evidence, 'region_id': region,
                        'image_sha256': image, 'contract_sha256': contract, 'binding_sha256': binding,
                        'observation_kind': kind, 'source_ref': 'observations/' + sha + '.json',
                        'snippet': display[:MAX_SNIPPET]})
            return QueryResult(tuple(hits), sum(not current[row[0]] for row in rows), self.generation, truncated)
        except sqlite3.Error as exc:
            timed_out = time.monotonic() >= self._deadline
            self.close()
            if timed_out:
                raise TimeoutError('observation query deadline exceeded') from exc
            raise QueryUnavailable('observation query execution failed') from exc
        except BaseException:
            self.close()
            raise
