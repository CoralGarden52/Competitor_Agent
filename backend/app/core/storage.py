from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
import re
import textwrap
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from app.core.config import get_config

from app.core.models import (
    ApprovalPolicy,
    AnalyzeHandoff,
    CollectHandoff,
    DraftHandoff,
    EventRecord,
    FieldRiskProfile,
    LLMCallTrace,
    PolicyAuditRecord,
    PolicyDecision,
    PlanHandoff,
    ProposalStatus,
    RunState,
    RunSummary,
    SchemaEvolutionProposal,
    StageName,
)

class _CompatCursor:
    def __init__(self, cursor: Any, *, prefetched_row: dict[str, Any] | None = None, lastrowid: int | None = None):
        self._cursor = cursor
        self._prefetched_row = prefetched_row
        self.lastrowid = lastrowid

    def fetchone(self) -> dict[str, Any] | None:
        if self._prefetched_row is not None:
            row = self._prefetched_row
            self._prefetched_row = None
            return row
        return self._cursor.fetchone()

    def fetchall(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if self._prefetched_row is not None:
            rows.append(self._prefetched_row)
            self._prefetched_row = None
        rows.extend(self._cursor.fetchall())
        return rows


class _CompatConnection:
    _REPLACE_CONFLICT_COLUMNS = {
        'conversation_memory': ('conversation_id',),
        'evidence_raw_contents': ('content_hash',),
        'llm_calls': ('trace_id',),
    }
    _IGNORE_CONFLICT_COLUMNS = {
        'run_comparison_corpus_links': ('run_id', 'corpus_id', 'usage_type'),
    }
    _RETURNING_ID_TABLES = {'agent_runs', 'events'}

    def __init__(self, conninfo: str):
        self._raw = psycopg.connect(conninfo, row_factory=dict_row)

    def __enter__(self) -> _CompatConnection:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._raw.commit()
        else:
            self._raw.rollback()
        self._raw.close()

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> _CompatCursor:
        normalized_sql, returning_id = self._normalize_sql(sql)
        cursor = self._raw.execute(normalized_sql, tuple(params or ()))
        prefetched_row = None
        lastrowid = None
        if returning_id:
            prefetched_row = cursor.fetchone()
            if prefetched_row is not None:
                lastrowid = int(prefetched_row['id'])
        return _CompatCursor(cursor, prefetched_row=prefetched_row, lastrowid=lastrowid)

    def _normalize_sql(self, sql: str) -> tuple[str, bool]:
        normalized = textwrap.dedent(sql).strip()
        normalized = re.sub(r'\bid\s+INTEGER PRIMARY KEY AUTOINCREMENT\b', 'id BIGSERIAL PRIMARY KEY', normalized, flags=re.IGNORECASE)
        normalized = re.sub(r'\bINTEGER PRIMARY KEY AUTOINCREMENT\b', 'BIGSERIAL PRIMARY KEY', normalized, flags=re.IGNORECASE)

        replace_match = re.match(
            r'^INSERT\s+OR\s+REPLACE\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\((.*?)\)\s*VALUES\s*\((.*?)\)$',
            normalized,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if replace_match:
            table = replace_match.group(1)
            columns_block = replace_match.group(2)
            values_block = replace_match.group(3)
            conflict_columns = self._REPLACE_CONFLICT_COLUMNS.get(table)
            if conflict_columns:
                columns = [item.strip() for item in columns_block.split(',')]
                update_columns = [item for item in columns if item not in conflict_columns]
                assignments = ', '.join(f'{column}=excluded.{column}' for column in update_columns)
                normalized = (
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({values_block}) "
                    f"ON CONFLICT ({', '.join(conflict_columns)}) DO UPDATE SET {assignments}"
                )

        ignore_match = re.match(
            r'^INSERT\s+OR\s+IGNORE\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\((.*?)\)\s*VALUES\s*\((.*?)\)$',
            normalized,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if ignore_match:
            table = ignore_match.group(1)
            columns_block = ignore_match.group(2)
            values_block = ignore_match.group(3)
            conflict_columns = self._IGNORE_CONFLICT_COLUMNS.get(table)
            if conflict_columns:
                normalized = (
                    f"INSERT INTO {table} ({columns_block}) VALUES ({values_block}) "
                    f"ON CONFLICT ({', '.join(conflict_columns)}) DO NOTHING"
                )

        returning_id = False
        insert_match = re.match(r'^INSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)\b', normalized, flags=re.IGNORECASE)
        if insert_match:
            table = insert_match.group(1).lower()
            if table in self._RETURNING_ID_TABLES and 'RETURNING' not in normalized.upper():
                normalized = f'{normalized} RETURNING id'
                returning_id = True

        normalized = normalized.replace('?', '%s')
        return normalized, returning_id


class PostgresStore:
    def __init__(self, conninfo: str | Path | None = None, cache_backend: Any | None = None):
        if isinstance(conninfo, Path):
            conninfo = None
        self.conninfo = str(conninfo or get_config().postgres_dsn)
        self.cache = cache_backend
        self._ensure_database_exists()
        self._init_db()

    def set_cache_backend(self, cache_backend: Any | None) -> None:
        self.cache = cache_backend

    def _ensure_database_exists(self) -> None:
        conn = psycopg.conninfo.conninfo_to_dict(self.conninfo)
        target_db = str(conn.get('dbname') or '')
        if not target_db:
            raise ValueError('postgres database name is required')
        admin_conninfo = psycopg.conninfo.make_conninfo(
            host=conn.get('host'),
            port=conn.get('port'),
            user=conn.get('user'),
            password=conn.get('password'),
            dbname='postgres',
        )
        with psycopg.connect(admin_conninfo, autocommit=True) as admin:
            row = admin.execute('SELECT 1 FROM pg_database WHERE datname = %s', (target_db,)).fetchone()
            if row is None:
                admin.execute(f'CREATE DATABASE "{target_db}"')

    def _connect(self) -> _CompatConnection:
        return _CompatConnection(self.conninfo)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    industry TEXT NOT NULL,
                    status TEXT NOT NULL,
                    competitor_count INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS schema_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    industry TEXT NOT NULL,
                    version TEXT NOT NULL,
                    required_extension_fields_json TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(industry, version)
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS schema_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    industry TEXT NOT NULL,
                    proposal_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS schema_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS schema_activations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT NOT NULL,
                    industry TEXT NOT NULL,
                    version TEXT NOT NULL,
                    activated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS schema_approval_policies (
                    policy_id TEXT PRIMARY KEY,
                    industry TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    priority INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS schema_field_risk_profiles (
                    profile_id TEXT PRIMARY KEY,
                    industry TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    risk_level TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(industry, field_name)
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS schema_policy_audits (
                    audit_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    industry TEXT NOT NULL,
                    matched_policy_id TEXT,
                    decision TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    risk_summary_json TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS evidence_raw_contents (
                    content_hash TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS web_page_cache (
                    url TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL,
                    http_status INTEGER,
                    etag TEXT,
                    last_modified TEXT
                )
                '''
            )
            conn.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_web_page_cache_last_checked_at
                ON web_page_cache(last_checked_at)
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    duration_ms INTEGER,
                    error_text TEXT
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS agent_io (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    io_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS run_checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS manual_interventions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS stage_handoffs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    handoff_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_stage_handoffs_run_stage_attempt
                ON stage_handoffs(run_id, stage, attempt, id)
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS llm_calls (
                    trace_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    node_name TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    trace_name TEXT NOT NULL,
                    model TEXT NOT NULL,
                    status TEXT NOT NULL,
                    system_prompt TEXT NOT NULL,
                    user_payload_json TEXT NOT NULL,
                    raw_response_json TEXT NOT NULL,
                    parsed_response_json TEXT NOT NULL,
                    error_reason TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    finish_reason TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    usage_source TEXT NOT NULL,
                    usage_details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS comparison_corpus_documents (
                    corpus_id TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    topic_key TEXT NOT NULL,
                    industry TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    query TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    published_at TEXT,
                    date_confidence TEXT NOT NULL,
                    source_provider TEXT NOT NULL,
                    llm_extract_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_url, content_hash)
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS run_comparison_corpus_links (
                    run_id TEXT NOT NULL,
                    corpus_id TEXT NOT NULL,
                    usage_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, corpus_id, usage_type)
                )
                '''
            )
            conn.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_comparison_corpus_topic_industry
                ON comparison_corpus_documents(topic_key, industry, updated_at)
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS subagent_runs (
                    subagent_id TEXT PRIMARY KEY,
                    parent_run_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    competitor TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    seed_queries_json TEXT NOT NULL,
                    budget_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    tool_history_json TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    error_message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS run_conversations (
                    conversation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    turn_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    allow_web_collect INTEGER NOT NULL,
                    auto_apply INTEGER NOT NULL,
                    user_message TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS conversation_memory (
                    conversation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    short_window_json TEXT NOT NULL,
                    mid_summary TEXT NOT NULL,
                    long_archive_refs_json TEXT NOT NULL,
                    next_work_memory TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                '''
            )
            conn.execute(
                '''
                CREATE TABLE IF NOT EXISTS report_revisions (
                    revision_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    before_hash TEXT NOT NULL,
                    after_hash TEXT NOT NULL,
                    patch_summary TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source_refs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                '''
            )
            self._ensure_column(conn, 'conversation_turns', 'allow_web_collect', 'INTEGER NOT NULL DEFAULT 1')
            self._ensure_column(conn, 'conversation_turns', 'auto_apply', 'INTEGER NOT NULL DEFAULT 1')
            self._ensure_column(conn, 'conversation_memory', 'short_window_json', "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, 'conversation_memory', 'mid_summary', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, 'conversation_memory', 'long_archive_refs_json', "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, 'conversation_memory', 'next_work_memory', "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, 'report_revisions', 'reason', "TEXT NOT NULL DEFAULT ''")
            conn.execute('CREATE INDEX IF NOT EXISTS idx_run_conversations_run ON run_conversations(run_id, updated_at)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_conversation_messages_conversation ON conversation_messages(conversation_id, created_at)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_conversation_turns_conversation ON conversation_turns(conversation_id, created_at)')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_report_revisions_run ON report_revisions(run_id, created_at)')
            conn.execute(
                '''
                CREATE INDEX IF NOT EXISTS idx_llm_calls_run_node_attempt
                ON llm_calls(run_id, node_name, attempt, created_at)
                '''
            )
            self._seed_default_schema_versions(conn)
            self._seed_default_policies(conn)
            self._seed_default_field_risks(conn)

    def _ensure_column(self, conn: _CompatConnection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(
            '''
            SELECT column_name AS name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ''',
            (table,),
        ).fetchall()
        if any(str(row['name']) == column for row in rows):
            return
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')

    def _seed_default_schema_versions(self, conn: _CompatConnection) -> None:
        defaults = {
            'saas': ('v1', ['deployment_model', 'compliance_support']),
            'ecommerce': ('v1', ['fulfillment_capability', 'seller_ecosystem']),
        }
        now = datetime.now(UTC).isoformat()
        for industry, payload in defaults.items():
            version, fields = payload
            row = conn.execute(
                'SELECT 1 FROM schema_versions WHERE industry = ? AND version = ?',
                (industry, version),
            ).fetchone()
            if row:
                continue
            conn.execute(
                '''
                INSERT INTO schema_versions (industry, version, required_extension_fields_json, is_active, created_at)
                VALUES (?, ?, ?, 1, ?)
                ''',
                (industry, version, json.dumps(fields, ensure_ascii=False), now),
            )

    def _seed_default_policies(self, conn: _CompatConnection) -> None:
        now = datetime.now(UTC).isoformat()
        defaults = [
            ApprovalPolicy(
                policy_id='pol_global_safe',
                industry='global',
                priority=50,
                max_fields=4,
                max_qa_failures=2,
                max_allowed_risk='medium',
                denied_scopes=['report'],
                decision=PolicyDecision.approved,
                notes='Global safe default auto-approve policy',
            ),
            ApprovalPolicy(
                policy_id='pol_saas_balanced',
                industry='saas',
                priority=10,
                max_fields=6,
                max_qa_failures=3,
                max_allowed_risk='medium',
                denied_scopes=[],
                decision=PolicyDecision.approved,
                notes='SaaS domain balanced policy',
            ),
            ApprovalPolicy(
                policy_id='pol_ecommerce_strict',
                industry='ecommerce',
                priority=10,
                max_fields=4,
                max_qa_failures=2,
                max_allowed_risk='low',
                denied_scopes=['report'],
                decision=PolicyDecision.approved,
                notes='Ecommerce strict policy',
            ),
        ]
        for policy in defaults:
            row = conn.execute('SELECT 1 FROM schema_approval_policies WHERE policy_id = ?', (policy.policy_id,)).fetchone()
            if row:
                continue
            conn.execute(
                '''
                INSERT INTO schema_approval_policies (policy_id, industry, policy_json, enabled, priority, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    policy.policy_id,
                    policy.industry,
                    policy.model_dump_json(),
                    1 if policy.enabled else 0,
                    policy.priority,
                    now,
                    now,
                ),
            )

    def _seed_default_field_risks(self, conn: _CompatConnection) -> None:
        now = datetime.now(UTC).isoformat()
        defaults = [
            FieldRiskProfile(profile_id='frp_global_compliance', industry='global', field_name='compliance_support', risk_level='high', notes='Compliance claims are high risk'),
            FieldRiskProfile(profile_id='frp_global_deployment', industry='global', field_name='deployment_model', risk_level='medium', notes='Deployment details medium risk'),
            FieldRiskProfile(profile_id='frp_global_fulfillment', industry='global', field_name='fulfillment_capability', risk_level='medium', notes='Fulfillment claims medium risk'),
            FieldRiskProfile(profile_id='frp_global_seller', industry='global', field_name='seller_ecosystem', risk_level='low', notes='Seller ecosystem low risk'),
        ]
        for item in defaults:
            row = conn.execute(
                'SELECT 1 FROM schema_field_risk_profiles WHERE industry = ? AND field_name = ?',
                (item.industry, item.field_name),
            ).fetchone()
            if row:
                continue
            conn.execute(
                '''
                INSERT INTO schema_field_risk_profiles (profile_id, industry, field_name, risk_level, profile_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    item.profile_id,
                    item.industry,
                    item.field_name,
                    item.risk_level.value,
                    item.model_dump_json(),
                    now,
                    now,
                ),
            )

    def save_state(self, state: RunState) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            existing = conn.execute('SELECT run_id, created_at, state_json FROM runs WHERE run_id = ?', (state.run_id,)).fetchone()
            created_at = existing['created_at'] if existing else now
            if existing and not str(state.task_summary or '').strip():
                try:
                    existing_payload = json.loads(existing.get('state_json') or '{}')
                except Exception:
                    existing_payload = {}
                if isinstance(existing_payload, dict):
                    existing_summary = str(existing_payload.get('task_summary', '') or '').strip()
                    if existing_summary:
                        state.task_summary = existing_summary
            conn.execute(
                '''
                INSERT INTO runs (run_id, industry, status, competitor_count, state_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    industry=excluded.industry,
                    status=excluded.status,
                    competitor_count=excluded.competitor_count,
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                ''',
                (
                    state.run_id,
                    state.industry,
                    state.status,
                    len(state.effective_analysis_subject_names()),
                    state.model_dump_json(),
                    created_at,
                    now,
                ),
            )
        if self.cache is not None:
            state_payload = state.model_dump(mode='json')
            self.cache.set_run_state(state.run_id, state_payload)
            self.cache.set_run_summary(
                state.run_id,
                {
                    'run_id': state.run_id,
                    'industry': state.industry,
                    'status': state.status,
                    'competitor_count': len(state.effective_analysis_subject_names()),
                    'user_prompt': state.user_prompt,
                    'task_summary': state.task_summary,
                    'created_at': created_at,
                    'updated_at': now,
                },
            )
            self.cache.invalidate_runs_lists()
            self.cache.invalidate_workspace(state.run_id)
            self.cache.invalidate_chat_payload(state.run_id)

    def append_event(self, event: EventRecord) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                'INSERT INTO events (run_id, stage, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)',
                (
                    event.run_id,
                    event.stage.value,
                    event.event_type,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.created_at.isoformat(),
                ),
            )
        event_id = int(cur.lastrowid or 0)
        if self.cache is not None:
            self.cache.invalidate_workspace(event.run_id)
            self.cache.publish_run_event(
                event.run_id,
                {
                    'event_id': event_id,
                    'stage': event.stage.value,
                    'event_type': event.event_type,
                    'payload': event.payload,
                    'created_at': event.created_at.isoformat(),
                },
            )

    def get_state(self, run_id: str) -> RunState | None:
        if self.cache is not None:
            cached = self.cache.get_run_state(run_id)
            if isinstance(cached, dict):
                try:
                    return RunState.model_validate(cached)
                except Exception:
                    pass
        with self._connect() as conn:
            row = conn.execute('SELECT state_json FROM runs WHERE run_id = ?', (run_id,)).fetchone()
        if row is None:
            return None
        state = RunState.model_validate_json(row['state_json'])
        if self.cache is not None:
            self.cache.set_run_state(run_id, state.model_dump(mode='json'))
        return state

    def get_run_state(self, run_id: str) -> RunState | None:
        # Backward-compatible alias used by workflow action tools and tool handlers.
        return self.get_state(run_id)

    def update_run_task_summary(self, run_id: str, task_summary: str) -> None:
        cleaned = str(task_summary or '').strip()
        if not run_id or not cleaned:
            return
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            row = conn.execute('SELECT state_json FROM runs WHERE run_id = ?', (run_id,)).fetchone()
            if row is None:
                return
            try:
                payload = json.loads(row['state_json'] or '{}')
            except Exception:
                return
            if not isinstance(payload, dict):
                return
            payload['task_summary'] = cleaned
            conn.execute(
                'UPDATE runs SET state_json = ?, updated_at = ? WHERE run_id = ?',
                (json.dumps(payload, ensure_ascii=False), now, run_id),
            )
        if self.cache is not None:
            cached_state = self.cache.get_run_state(run_id)
            if isinstance(cached_state, dict):
                cached_state['task_summary'] = cleaned
                self.cache.set_run_state(run_id, cached_state)
            cached_summary = self.cache.get_run_summary(run_id)
            if isinstance(cached_summary, dict):
                cached_summary['task_summary'] = cleaned
                cached_summary['updated_at'] = now
                self.cache.set_run_summary(run_id, cached_summary)
            else:
                state = self.get_state(run_id)
                if state is not None:
                    self.cache.set_run_summary(
                        run_id,
                        {
                            'run_id': state.run_id,
                            'industry': state.industry,
                            'status': state.status,
                            'competitor_count': len(state.effective_analysis_subject_names()),
                            'user_prompt': state.user_prompt,
                            'task_summary': state.task_summary,
                            'created_at': now,
                            'updated_at': now,
                        },
                    )
            self.cache.invalidate_runs_lists()

    def list_runs(self, limit: int = 20) -> list[RunSummary]:
        if self.cache is not None:
            cached = self.cache.get_runs_list(limit)
            if isinstance(cached, list):
                try:
                    return [RunSummary.model_validate(item) for item in cached if isinstance(item, dict)]
                except Exception:
                    pass
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT run_id, industry, status, competitor_count, state_json, created_at, updated_at FROM runs ORDER BY updated_at DESC LIMIT ?',
                (limit,),
            ).fetchall()
        def _extract_user_prompt(state_json: str) -> str:
            try:
                payload = json.loads(state_json or '{}')
                return str(payload.get('user_prompt', '') or '').strip()
            except Exception:
                return ''
        def _extract_task_summary(state_json: str) -> str:
            try:
                payload = json.loads(state_json or '{}')
                return str(payload.get('task_summary', '') or '').strip()
            except Exception:
                return ''
        output = [
            RunSummary(
                run_id=row['run_id'],
                industry=row['industry'],
                status=row['status'],
                competitor_count=row['competitor_count'],
                user_prompt=_extract_user_prompt(row['state_json']),
                task_summary=_extract_task_summary(row['state_json']),
                created_at=datetime.fromisoformat(row['created_at']),
                updated_at=datetime.fromisoformat(row['updated_at']),
            )
            for row in rows
        ]
        if self.cache is not None:
            self.cache.set_runs_list(limit, [item.model_dump(mode='json') for item in output])
        return output

    def list_events(self, run_id: str, *, after_id: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        sql = 'SELECT id, stage, event_type, payload_json, created_at FROM events WHERE run_id = ?'
        params: list[Any] = [run_id]
        if after_id > 0:
            sql += ' AND id > ?'
            params.append(after_id)
        sql += ' ORDER BY id ASC'
        if limit is not None:
            sql += ' LIMIT ?'
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            output.append(
                {
                    'event_id': row['id'],
                    'stage': row['stage'],
                    'event_type': row['event_type'],
                    'payload': json.loads(row['payload_json']),
                    'created_at': row['created_at'],
                }
            )
        return output

    def append_stage_event(self, run_id: str, stage: StageName, event_type: str, payload: dict[str, Any]) -> None:
        self.append_event(EventRecord(run_id=run_id, stage=stage, event_type=event_type, payload=payload))

    def get_active_domain_schema(self, industry: str) -> dict[str, Any]:
        key = industry.strip().lower()
        with self._connect() as conn:
            row = conn.execute(
                '''
                SELECT version, required_extension_fields_json
                FROM schema_versions
                WHERE industry = ? AND is_active = 1
                ORDER BY id DESC LIMIT 1
                ''',
                (key,),
            ).fetchone()
        if row is None:
            return {'industry': key or 'generic', 'version': 'v1', 'required_extension_fields': []}
        return {
            'industry': key,
            'version': row['version'],
            'required_extension_fields': json.loads(row['required_extension_fields_json']),
        }

    def list_active_domain_schemas(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                '''
                SELECT industry, version, required_extension_fields_json
                FROM schema_versions
                WHERE is_active = 1
                ORDER BY industry ASC
                '''
            ).fetchall()
        return [
            {
                'industry': row['industry'],
                'version': row['version'],
                'required_extension_fields': json.loads(row['required_extension_fields_json']),
            }
            for row in rows
        ]

    def save_proposal(self, proposal: SchemaEvolutionProposal) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO schema_proposals (proposal_id, industry, proposal_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    proposal_json=excluded.proposal_json,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                ''',
                (
                    proposal.proposal_id,
                    proposal.industry,
                    proposal.model_dump_json(),
                    proposal.status.value,
                    now,
                    now,
                ),
            )

    def list_proposals(self, status: ProposalStatus | None = None) -> list[SchemaEvolutionProposal]:
        sql = 'SELECT proposal_json FROM schema_proposals'
        params: tuple[Any, ...] = ()
        if status is not None:
            sql += ' WHERE status = ?'
            params = (status.value,)
        sql += ' ORDER BY updated_at DESC'
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [SchemaEvolutionProposal.model_validate_json(row['proposal_json']) for row in rows]

    def get_proposal(self, proposal_id: str) -> SchemaEvolutionProposal | None:
        with self._connect() as conn:
            row = conn.execute('SELECT proposal_json FROM schema_proposals WHERE proposal_id = ?', (proposal_id,)).fetchone()
        if row is None:
            return None
        return SchemaEvolutionProposal.model_validate_json(row['proposal_json'])

    def review_proposal(self, proposal: SchemaEvolutionProposal, *, reviewer: str, decision: ProposalStatus, notes: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                'UPDATE schema_proposals SET proposal_json = ?, status = ?, updated_at = ? WHERE proposal_id = ?',
                (proposal.model_dump_json(), proposal.status.value, now, proposal.proposal_id),
            )
            conn.execute(
                '''
                INSERT INTO schema_reviews (proposal_id, decision, reviewer, notes, created_at)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (proposal.proposal_id, decision.value, reviewer, notes, now),
            )

    def activate_proposal(self, proposal: SchemaEvolutionProposal, *, activated_by: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute('UPDATE schema_versions SET is_active = 0 WHERE industry = ?', (proposal.industry,))
            conn.execute(
                '''
                INSERT INTO schema_versions (industry, version, required_extension_fields_json, is_active, created_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(industry, version) DO UPDATE SET
                    required_extension_fields_json=excluded.required_extension_fields_json,
                    is_active=1
                ''',
                (proposal.industry, proposal.target_version, json.dumps(proposal.suggested_fields, ensure_ascii=False), now),
            )
            conn.execute(
                '''
                INSERT INTO schema_activations (proposal_id, industry, version, activated_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (proposal.proposal_id, proposal.industry, proposal.target_version, activated_by, now),
            )
            conn.execute(
                'UPDATE schema_proposals SET proposal_json = ?, status = ?, updated_at = ? WHERE proposal_id = ?',
                (proposal.model_dump_json(), proposal.status.value, now, proposal.proposal_id),
            )

    def get_proposal_audit(self, proposal_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                '''
                SELECT decision, reviewer, notes, created_at
                FROM schema_reviews
                WHERE proposal_id = ?
                ORDER BY id ASC
                ''',
                (proposal_id,),
            ).fetchall()
        return [
            {
                'decision': row['decision'],
                'reviewer': row['reviewer'],
                'notes': row['notes'],
                'created_at': row['created_at'],
            }
            for row in rows
        ]

    def list_policies(self, industry: str | None = None) -> list[ApprovalPolicy]:
        sql = 'SELECT policy_json FROM schema_approval_policies'
        params: tuple[Any, ...] = ()
        if industry:
            sql += ' WHERE industry IN (?, ?)'
            params = (industry.strip().lower(), 'global')
        sql += ' ORDER BY priority ASC'
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ApprovalPolicy.model_validate_json(row['policy_json']) for row in rows]

    def upsert_policy(self, policy: ApprovalPolicy) -> ApprovalPolicy:
        now = datetime.now(UTC).isoformat()
        policy.updated_at = datetime.fromisoformat(now)
        if not policy.created_at:
            policy.created_at = datetime.fromisoformat(now)
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO schema_approval_policies (policy_id, industry, policy_json, enabled, priority, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    industry=excluded.industry,
                    policy_json=excluded.policy_json,
                    enabled=excluded.enabled,
                    priority=excluded.priority,
                    updated_at=excluded.updated_at
                ''',
                (
                    policy.policy_id,
                    policy.industry.strip().lower(),
                    policy.model_dump_json(),
                    1 if policy.enabled else 0,
                    policy.priority,
                    policy.created_at.isoformat(),
                    policy.updated_at.isoformat(),
                ),
            )
        return policy

    def list_field_risks(self, industry: str | None = None) -> list[FieldRiskProfile]:
        sql = 'SELECT profile_json FROM schema_field_risk_profiles'
        params: tuple[Any, ...] = ()
        if industry:
            sql += ' WHERE industry IN (?, ?)'
            params = (industry.strip().lower(), 'global')
        sql += ' ORDER BY industry ASC, field_name ASC'
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [FieldRiskProfile.model_validate_json(row['profile_json']) for row in rows]

    def upsert_field_risk(self, profile: FieldRiskProfile) -> FieldRiskProfile:
        now = datetime.now(UTC).isoformat()
        profile.updated_at = datetime.fromisoformat(now)
        if not profile.created_at:
            profile.created_at = datetime.fromisoformat(now)
        key_industry = profile.industry.strip().lower()
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO schema_field_risk_profiles (profile_id, industry, field_name, risk_level, profile_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(industry, field_name) DO UPDATE SET
                    risk_level=excluded.risk_level,
                    profile_json=excluded.profile_json,
                    updated_at=excluded.updated_at
                ''',
                (
                    profile.profile_id,
                    key_industry,
                    profile.field_name,
                    profile.risk_level.value,
                    profile.model_dump_json(),
                    profile.created_at.isoformat(),
                    profile.updated_at.isoformat(),
                ),
            )
        return profile

    def save_policy_audit(self, audit: PolicyAuditRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO schema_policy_audits (audit_id, proposal_id, industry, matched_policy_id, decision, reason, risk_summary_json, context_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    audit.audit_id,
                    audit.proposal_id,
                    audit.industry,
                    audit.matched_policy_id,
                    audit.decision.value,
                    audit.reason,
                    json.dumps(audit.risk_summary, ensure_ascii=False),
                    json.dumps(audit.context, ensure_ascii=False),
                    audit.created_at.isoformat(),
                ),
            )

    def list_policy_audits(self, proposal_id: str | None = None) -> list[PolicyAuditRecord]:
        sql = 'SELECT * FROM schema_policy_audits'
        params: tuple[Any, ...] = ()
        if proposal_id:
            sql += ' WHERE proposal_id = ?'
            params = (proposal_id,)
        sql += ' ORDER BY created_at DESC'
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        output: list[PolicyAuditRecord] = []
        for row in rows:
            output.append(
                PolicyAuditRecord(
                    audit_id=row['audit_id'],
                    proposal_id=row['proposal_id'],
                    industry=row['industry'],
                    matched_policy_id=row['matched_policy_id'],
                    decision=PolicyDecision(row['decision']),
                    reason=row['reason'],
                    risk_summary=json.loads(row['risk_summary_json']),
                    context=json.loads(row['context_json']),
                    created_at=datetime.fromisoformat(row['created_at']),
                )
            )
        return output

    def index_raw_evidence_content(self, *, run_id: str, evidence_id: str, source_url: str, content_hash: str, local_path: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT OR REPLACE INTO evidence_raw_contents (content_hash, run_id, evidence_id, source_url, local_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (content_hash, run_id, evidence_id, source_url, local_path, now),
            )

    def get_cached_page(self, url: str) -> dict[str, Any] | None:
        if self.cache is not None:
            cached = self.cache.get_webpage(url)
            if isinstance(cached, dict):
                return cached
        with self._connect() as conn:
            row = conn.execute(
                '''
                SELECT url, content, content_hash, source_provider, fetched_at, last_checked_at, http_status, etag, last_modified
                FROM web_page_cache
                WHERE url = ?
                ''',
                (url,),
            ).fetchone()
        if row is None:
            return None
        payload = {
            'url': row['url'],
            'content': row['content'],
            'content_hash': row['content_hash'],
            'source_provider': row['source_provider'],
            'fetched_at': row['fetched_at'],
            'last_checked_at': row['last_checked_at'],
            'http_status': row['http_status'],
            'etag': row['etag'],
            'last_modified': row['last_modified'],
        }
        if self.cache is not None:
            self.cache.set_webpage(url, payload)
        return payload

    def upsert_cached_page(
        self,
        *,
        url: str,
        content: str,
        content_hash: str,
        source_provider: str,
        http_status: int = 200,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            existing = conn.execute('SELECT fetched_at FROM web_page_cache WHERE url = ?', (url,)).fetchone()
            fetched_at = existing['fetched_at'] if existing else now
            conn.execute(
                '''
                INSERT INTO web_page_cache (url, content, content_hash, source_provider, fetched_at, last_checked_at, http_status, etag, last_modified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    content=excluded.content,
                    content_hash=excluded.content_hash,
                    source_provider=excluded.source_provider,
                    last_checked_at=excluded.last_checked_at,
                    http_status=excluded.http_status,
                    etag=excluded.etag,
                    last_modified=excluded.last_modified
                ''',
                (url, content, content_hash, source_provider, fetched_at, now, http_status, etag, last_modified),
            )
        if self.cache is not None:
            self.cache.set_webpage(
                url,
                {
                    'url': url,
                    'content': content,
                    'content_hash': content_hash,
                    'source_provider': source_provider,
                    'fetched_at': fetched_at,
                    'last_checked_at': now,
                    'http_status': http_status,
                    'etag': etag,
                    'last_modified': last_modified,
                },
            )

    def upsert_comparison_corpus_document(self, document: dict[str, Any]) -> str:
        now = datetime.now(UTC).isoformat()
        corpus_id = str(document.get('corpus_id', '') or f"corpus_{document.get('content_hash', '')[:12]}").strip()
        if not corpus_id:
            raise ValueError('comparison corpus document requires corpus_id or content_hash')
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO comparison_corpus_documents (
                    corpus_id, source_url, title, topic_key, industry, keywords_json, query,
                    summary, content, content_hash, published_at, date_confidence,
                    source_provider, llm_extract_json, fetched_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_url, content_hash) DO UPDATE SET
                    title=excluded.title,
                    topic_key=excluded.topic_key,
                    industry=excluded.industry,
                    keywords_json=excluded.keywords_json,
                    query=excluded.query,
                    summary=excluded.summary,
                    content=excluded.content,
                    published_at=excluded.published_at,
                    date_confidence=excluded.date_confidence,
                    source_provider=excluded.source_provider,
                    llm_extract_json=excluded.llm_extract_json,
                    updated_at=excluded.updated_at
                ''',
                (
                    corpus_id,
                    str(document.get('source_url', '')),
                    str(document.get('title', '')),
                    str(document.get('topic_key', '')),
                    str(document.get('industry', '')),
                    json.dumps(document.get('keywords', []), ensure_ascii=False),
                    str(document.get('query', '')),
                    str(document.get('summary', '')),
                    str(document.get('content', '')),
                    str(document.get('content_hash', '')),
                    str(document.get('published_at', '') or '') or None,
                    str(document.get('date_confidence', 'unknown') or 'unknown'),
                    str(document.get('source_provider', '')),
                    json.dumps(document.get('llm_extract', {}), ensure_ascii=False),
                    str(document.get('fetched_at', '') or now),
                    now,
                ),
            )
            row = conn.execute(
                'SELECT corpus_id FROM comparison_corpus_documents WHERE source_url = ? AND content_hash = ?',
                (str(document.get('source_url', '')), str(document.get('content_hash', ''))),
            ).fetchone()
        final_id = str(row['corpus_id']) if row is not None else corpus_id
        if self.cache is not None:
            self.cache.invalidate_corpus_industry(str(document.get('industry', '') or ''))
        return final_id

    def link_run_comparison_corpus(self, *, run_id: str, corpus_id: str, usage_type: str = 'plan_selected') -> None:
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT OR IGNORE INTO run_comparison_corpus_links (run_id, corpus_id, usage_type, created_at)
                VALUES (?, ?, ?, ?)
                ''',
                (run_id, corpus_id, usage_type, datetime.now(UTC).isoformat()),
            )

    def search_comparison_corpus(
        self,
        *,
        topic_key: str = '',
        industry: str = '',
        keywords: list[str] | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        keyword_items = [str(item).strip() for item in (keywords or []) if str(item).strip()]
        if self.cache is not None and industry.strip() and keyword_items:
            cached = self.cache.get_corpus_search(industry=industry.strip(), keywords=keyword_items[:6], limit=limit)
            if isinstance(cached, list):
                return [item for item in cached if isinstance(item, dict)]
        clauses: list[str] = []
        params: list[Any] = []
        if topic_key.strip():
            clauses.append('topic_key = ?')
            params.append(topic_key.strip())
        if industry.strip():
            clauses.append('(industry = ? OR industry = ?)')
            params.extend([industry.strip(), 'general'])
        if keyword_items:
            keyword_clauses = []
            for keyword in keyword_items[:6]:
                keyword_clauses.append('(keywords_json LIKE ? OR title LIKE ? OR summary LIKE ?)')
                token = f'%{keyword}%'
                params.extend([token, token, token])
            clauses.append(f"({' OR '.join(keyword_clauses)})")
        sql = 'SELECT * FROM comparison_corpus_documents'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY updated_at DESC LIMIT ?'
        params.append(max(1, min(int(limit), 20)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        output = [
            {
                'corpus_id': row['corpus_id'],
                'source_url': row['source_url'],
                'title': row['title'],
                'topic_key': row['topic_key'],
                'industry': row['industry'],
                'keywords': json.loads(row['keywords_json']),
                'query': row['query'],
                'summary': row['summary'],
                'content': row['content'],
                'content_hash': row['content_hash'],
                'published_at': row['published_at'] or '',
                'date_confidence': row['date_confidence'],
                'source_provider': row['source_provider'],
                'llm_extract': json.loads(row['llm_extract_json']),
                'fetched_at': row['fetched_at'],
            }
            for row in rows
        ]
        if self.cache is not None and industry.strip() and keyword_items:
            self.cache.set_corpus_search(industry=industry.strip(), keywords=keyword_items[:6], limit=limit, payload=output)
        return output

    def get_or_create_conversation(self, run_id: str) -> dict[str, Any]:
        if self.cache is not None:
            cached = self.cache.get_conversation(run_id)
            if isinstance(cached, dict):
                return cached
        now_s = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                '''
                SELECT conversation_id, run_id, title, created_at, updated_at
                FROM run_conversations
                WHERE run_id = ?
                ORDER BY created_at ASC
                LIMIT 1
                ''',
                (run_id,),
            ).fetchone()
            if row is None:
                conversation_id = f'conv_{uuid4().hex[:12]}'
                conn.execute(
                    '''
                    INSERT INTO run_conversations (conversation_id, run_id, title, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ''',
                    (conversation_id, run_id, 'default', now_s, now_s),
                )
                conn.execute(
                    '''
                    INSERT OR REPLACE INTO conversation_memory
                    (conversation_id, run_id, short_window_json, mid_summary, long_archive_refs_json, next_work_memory, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (conversation_id, run_id, '[]', '', '[]', '', now_s),
                )
                payload = {
                    'conversation_id': conversation_id,
                    'run_id': run_id,
                    'title': 'default',
                    'created_at': now_s,
                    'updated_at': now_s,
                }
                if self.cache is not None:
                    self.cache.set_conversation(run_id, payload)
                    self.cache.invalidate_chat_payload(run_id)
                return payload
        payload = dict(row)
        if self.cache is not None:
            self.cache.set_conversation(run_id, payload)
        return payload

    def create_conversation_turn(
        self,
        *,
        run_id: str,
        conversation_id: str,
        mode: str,
        allow_web_collect: bool,
        auto_apply: bool,
        user_message: str,
        status: str = 'queued',
    ) -> dict[str, Any]:
        now_s = datetime.now(UTC).isoformat()
        turn_id = f'turn_{uuid4().hex[:12]}'
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO conversation_turns
                (turn_id, conversation_id, run_id, status, mode, allow_web_collect, auto_apply, user_message, result_json, error_message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    turn_id,
                    conversation_id,
                    run_id,
                    status,
                    mode,
                    1 if allow_web_collect else 0,
                    1 if auto_apply else 0,
                    user_message,
                    '{}',
                    '',
                    now_s,
                    now_s,
                ),
            )
            conn.execute('UPDATE run_conversations SET updated_at = ? WHERE conversation_id = ?', (now_s, conversation_id))
        if self.cache is not None:
            self.cache.set_conversation(
                run_id,
                {
                    'conversation_id': conversation_id,
                    'run_id': run_id,
                    'title': 'default',
                    'created_at': now_s,
                    'updated_at': now_s,
                },
            )
        payload = {
            'turn_id': turn_id,
            'conversation_id': conversation_id,
            'run_id': run_id,
            'status': status,
            'mode': mode,
            'allow_web_collect': allow_web_collect,
            'auto_apply': auto_apply,
            'user_message': user_message,
            'result': {},
            'error_message': '',
            'created_at': now_s,
            'updated_at': now_s,
        }
        if self.cache is not None:
            self.cache.set_turn_result(turn_id, payload)
            self.cache.invalidate_chat_payload(run_id)
        return payload

    def update_conversation_turn(
        self,
        *,
        turn_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error_message: str = '',
    ) -> None:
        now_s = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                '''
                UPDATE conversation_turns
                SET status = ?, result_json = COALESCE(?, result_json), error_message = ?, updated_at = ?
                WHERE turn_id = ?
                ''',
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error_message,
                    now_s,
                    turn_id,
                ),
            )
            row = conn.execute('SELECT conversation_id FROM conversation_turns WHERE turn_id = ?', (turn_id,)).fetchone()
            if row is not None:
                conn.execute('UPDATE run_conversations SET updated_at = ? WHERE conversation_id = ?', (now_s, row['conversation_id']))
        if self.cache is not None:
            turn = self._get_conversation_turn_from_db(turn_id)
            if turn is not None:
                self.cache.set_turn_result(turn_id, turn)
                self.cache.invalidate_chat_payload(str(turn.get('run_id', '') or ''))

    def append_conversation_message(
        self,
        *,
        run_id: str,
        conversation_id: str,
        turn_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now_s = datetime.now(UTC).isoformat()
        message_id = f'msg_{uuid4().hex[:12]}'
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO conversation_messages
                (message_id, conversation_id, run_id, turn_id, role, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    message_id,
                    conversation_id,
                    run_id,
                    turn_id,
                    role,
                    content,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now_s,
                ),
            )
            conn.execute('UPDATE run_conversations SET updated_at = ? WHERE conversation_id = ?', (now_s, conversation_id))
        payload = {
            'message_id': message_id,
            'conversation_id': conversation_id,
            'run_id': run_id,
            'turn_id': turn_id,
            'role': role,
            'content': content,
            'metadata': metadata or {},
            'created_at': now_s,
        }
        if self.cache is not None:
            self.cache.invalidate_chat_payload(run_id)
        return payload

    def list_conversation_messages(
        self,
        *,
        run_id: str,
        conversation_id: str = '',
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = '''
            SELECT message_id, conversation_id, run_id, turn_id, role, content, metadata_json, created_at
            FROM conversation_messages
            WHERE run_id = ?
        '''
        params: list[Any] = [run_id]
        if conversation_id:
            sql += ' AND conversation_id = ?'
            params.append(conversation_id)
        sql += ' ORDER BY created_at ASC LIMIT ?'
        params.append(max(1, min(int(limit), 1000)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                'message_id': row['message_id'],
                'conversation_id': row['conversation_id'],
                'run_id': row['run_id'],
                'turn_id': row['turn_id'],
                'role': row['role'],
                'content': row['content'],
                'metadata': json.loads(row['metadata_json'] or '{}'),
                'created_at': row['created_at'],
            }
            for row in rows
        ]

    def get_conversation_turn(self, turn_id: str) -> dict[str, Any] | None:
        if self.cache is not None:
            cached = self.cache.get_turn_result(turn_id)
            if isinstance(cached, dict):
                return cached
        payload = self._get_conversation_turn_from_db(turn_id)
        if payload is not None and self.cache is not None:
            self.cache.set_turn_result(turn_id, payload)
        return payload

    def _get_conversation_turn_from_db(self, turn_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                '''
                SELECT turn_id, conversation_id, run_id, status, mode, allow_web_collect, auto_apply,
                       user_message, result_json, error_message, created_at, updated_at
                FROM conversation_turns
                WHERE turn_id = ?
                ''',
                (turn_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            'turn_id': row['turn_id'],
            'conversation_id': row['conversation_id'],
            'run_id': row['run_id'],
            'status': row['status'],
            'mode': row['mode'],
            'allow_web_collect': bool(row['allow_web_collect']),
            'auto_apply': bool(row['auto_apply']),
            'user_message': row['user_message'],
            'result': json.loads(row['result_json'] or '{}'),
            'error_message': row['error_message'],
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
        }

    def list_conversation_turns(
        self,
        *,
        run_id: str,
        conversation_id: str = '',
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = '''
            SELECT turn_id
            FROM conversation_turns
            WHERE run_id = ?
        '''
        params: list[Any] = [run_id]
        if conversation_id:
            sql += ' AND conversation_id = ?'
            params.append(conversation_id)
        sql += ' ORDER BY created_at ASC LIMIT ?'
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [turn for row in rows if (turn := self.get_conversation_turn(row['turn_id'])) is not None]

    def get_conversation_memory(self, conversation_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                '''
                SELECT conversation_id, run_id, short_window_json, mid_summary, long_archive_refs_json, next_work_memory, updated_at
                FROM conversation_memory
                WHERE conversation_id = ?
                ''',
                (conversation_id,),
            ).fetchone()
        if row is None:
            return {
                'conversation_id': conversation_id,
                'run_id': '',
                'short_window': [],
                'mid_summary': '',
                'long_archive_refs': [],
                'next_work_memory': '',
                'updated_at': '',
            }
        return {
            'conversation_id': row['conversation_id'],
            'run_id': row['run_id'],
            'short_window': json.loads(row['short_window_json'] or '[]'),
            'mid_summary': row['mid_summary'],
            'long_archive_refs': json.loads(row['long_archive_refs_json'] or '[]'),
            'next_work_memory': row['next_work_memory'],
            'updated_at': row['updated_at'],
        }

    def save_conversation_memory(
        self,
        *,
        conversation_id: str,
        run_id: str,
        short_window: list[dict[str, Any]],
        mid_summary: str,
        long_archive_refs: list[dict[str, Any]],
        next_work_memory: str,
    ) -> None:
        now_s = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO conversation_memory
                (conversation_id, run_id, short_window_json, mid_summary, long_archive_refs_json, next_work_memory, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    run_id=excluded.run_id,
                    short_window_json=excluded.short_window_json,
                    mid_summary=excluded.mid_summary,
                    long_archive_refs_json=excluded.long_archive_refs_json,
                    next_work_memory=excluded.next_work_memory,
                    updated_at=excluded.updated_at
                ''',
                (
                    conversation_id,
                    run_id,
                    json.dumps(short_window, ensure_ascii=False),
                    mid_summary,
                    json.dumps(long_archive_refs, ensure_ascii=False),
                    next_work_memory,
                    now_s,
                ),
            )
        if self.cache is not None:
            self.cache.invalidate_chat_payload(run_id)

    def save_report_revision(
        self,
        *,
        run_id: str,
        conversation_id: str,
        turn_id: str,
        before_hash: str,
        after_hash: str,
        patch_summary: str,
        reason: str,
        source_refs: list[str],
    ) -> dict[str, Any]:
        now_s = datetime.now(UTC).isoformat()
        revision_id = f'rev_{uuid4().hex[:12]}'
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO report_revisions
                (revision_id, run_id, conversation_id, turn_id, before_hash, after_hash, patch_summary, reason, source_refs_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    revision_id,
                    run_id,
                    conversation_id,
                    turn_id,
                    before_hash,
                    after_hash,
                    patch_summary,
                    reason,
                    json.dumps(source_refs, ensure_ascii=False),
                    now_s,
                ),
            )
        payload = {
            'revision_id': revision_id,
            'run_id': run_id,
            'conversation_id': conversation_id,
            'turn_id': turn_id,
            'before_hash': before_hash,
            'after_hash': after_hash,
            'patch_summary': patch_summary,
            'reason': reason,
            'source_refs': source_refs,
            'created_at': now_s,
        }
        if self.cache is not None:
            self.cache.invalidate_chat_payload(run_id)
        return payload

    def list_report_revisions(self, *, run_id: str, conversation_id: str = '') -> list[dict[str, Any]]:
        sql = '''
            SELECT revision_id, run_id, conversation_id, turn_id, before_hash, after_hash,
                   patch_summary, reason, source_refs_json, created_at
            FROM report_revisions
            WHERE run_id = ?
        '''
        params: list[Any] = [run_id]
        if conversation_id:
            sql += ' AND conversation_id = ?'
            params.append(conversation_id)
        sql += ' ORDER BY created_at ASC'
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                'revision_id': row['revision_id'],
                'run_id': row['run_id'],
                'conversation_id': row['conversation_id'],
                'turn_id': row['turn_id'],
                'before_hash': row['before_hash'],
                'after_hash': row['after_hash'],
                'patch_summary': row['patch_summary'],
                'reason': row['reason'],
                'source_refs': json.loads(row['source_refs_json'] or '[]'),
                'created_at': row['created_at'],
            }
            for row in rows
        ]

    def trace_node_started(self, *, run_id: str, node_name: str, attempt: int) -> int:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                '''
                INSERT INTO agent_runs (run_id, node_name, attempt, status, started_at)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (run_id, node_name, attempt, 'running', now),
            )
            return int(cur.lastrowid)

    def trace_node_completed(self, *, trace_id: int, run_id: str, node_name: str, output_payload: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        now_s = now.isoformat()
        with self._connect() as conn:
            row = conn.execute('SELECT started_at FROM agent_runs WHERE id = ?', (trace_id,)).fetchone()
            duration_ms = None
            if row is not None:
                try:
                    started = datetime.fromisoformat(row['started_at'])
                    duration_ms = int((now - started).total_seconds() * 1000)
                except Exception:
                    duration_ms = None
            conn.execute(
                '''
                UPDATE agent_runs
                SET status = ?, ended_at = ?, duration_ms = ?
                WHERE id = ?
                ''',
                ('completed', now_s, duration_ms, trace_id),
            )
            conn.execute(
                '''
                INSERT INTO agent_io (run_id, node_name, io_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (run_id, node_name, 'output', json.dumps(output_payload, ensure_ascii=False), now_s),
            )

    def trace_node_failed(self, *, trace_id: int, error_text: str) -> None:
        now_s = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                '''
                UPDATE agent_runs
                SET status = ?, ended_at = ?, error_text = ?
                WHERE id = ?
                ''',
                ('failed', now_s, error_text[:2000], trace_id),
            )

    def trace_node_input(self, *, run_id: str, node_name: str, input_payload: dict[str, Any]) -> None:
        now_s = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO agent_io (run_id, node_name, io_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (run_id, node_name, 'input', json.dumps(input_payload, ensure_ascii=False), now_s),
            )

    def save_checkpoint(self, *, run_id: str, node_name: str, attempt: int, state: RunState) -> None:
        now_s = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO run_checkpoints (run_id, node_name, attempt, state_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ''',
                (run_id, node_name, attempt, state.model_dump_json(), now_s),
            )

    def latest_checkpoint(self, run_id: str) -> RunState | None:
        with self._connect() as conn:
            row = conn.execute(
                '''
                SELECT state_json FROM run_checkpoints
                WHERE run_id = ?
                ORDER BY id DESC
                LIMIT 1
                ''',
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return RunState.model_validate_json(row['state_json'])

    def replay_timeline(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                '''
                SELECT id, node_name, attempt, status, started_at, ended_at, duration_ms, error_text
                FROM agent_runs
                WHERE run_id = ?
                ORDER BY id ASC
                ''',
                (run_id,),
            ).fetchall()
        return [
            {
                'trace_id': row['id'],
                'node_name': row['node_name'],
                'attempt': row['attempt'],
                'status': row['status'],
                'started_at': row['started_at'],
                'ended_at': row['ended_at'],
                'duration_ms': row['duration_ms'],
                'error_text': row['error_text'],
            }
            for row in rows
        ]

    def save_stage_handoff(
        self,
        *,
        run_id: str,
        stage: StageName,
        attempt: int,
        handoff: PlanHandoff | CollectHandoff | AnalyzeHandoff | DraftHandoff,
    ) -> None:
        now_s = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO stage_handoffs (run_id, stage, attempt, handoff_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (
                    run_id,
                    stage.value,
                    attempt,
                    handoff.__class__.__name__,
                    handoff.model_dump_json(),
                    now_s,
                ),
            )

    def list_stage_handoffs(
        self,
        run_id: str,
        *,
        stage: str | None = None,
        attempt: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = 'SELECT stage, attempt, handoff_type, payload_json, created_at FROM stage_handoffs WHERE run_id = ?'
        params: list[Any] = [run_id]
        if stage is not None:
            sql += ' AND stage = ?'
            params.append(stage)
        if attempt is not None:
            sql += ' AND attempt = ?'
            params.append(attempt)
        sql += ' ORDER BY id ASC'
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                'stage': row['stage'],
                'attempt': row['attempt'],
                'handoff_type': row['handoff_type'],
                'payload': json.loads(row['payload_json']),
                'created_at': row['created_at'],
            }
            for row in rows
        ]

    def latest_stage_handoff(
        self,
        run_id: str,
        *,
        stage: StageName,
        attempt: int | None = None,
    ) -> PlanHandoff | CollectHandoff | AnalyzeHandoff | DraftHandoff | None:
        sql = 'SELECT handoff_type, payload_json FROM stage_handoffs WHERE run_id = ? AND stage = ?'
        params: list[Any] = [run_id, stage.value]
        if attempt is not None:
            sql += ' AND attempt = ?'
            params.append(attempt)
        sql += ' ORDER BY id DESC LIMIT 1'
        with self._connect() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        if row is None:
            return None
        type_name = row['handoff_type']
        if type_name == 'PlanHandoff':
            return PlanHandoff.model_validate_json(row['payload_json'])
        if type_name == 'CollectHandoff':
            return CollectHandoff.model_validate_json(row['payload_json'])
        if type_name == 'AnalyzeHandoff':
            return AnalyzeHandoff.model_validate_json(row['payload_json'])
        if type_name == 'DraftHandoff':
            return DraftHandoff.model_validate_json(row['payload_json'])
        return None

    def save_llm_call(self, trace: LLMCallTrace) -> None:
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT OR REPLACE INTO llm_calls (
                    trace_id, run_id, attempt, node_name, agent_name, trace_name, model, status,
                    system_prompt, user_payload_json, raw_response_json, parsed_response_json,
                    error_reason, error_message, finish_reason, latency_ms, prompt_tokens,
                    completion_tokens, total_tokens, usage_source, usage_details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    trace.trace_id,
                    trace.run_id,
                    trace.attempt,
                    trace.node_name,
                    trace.agent_name,
                    trace.trace_name,
                    trace.model,
                    trace.status,
                    trace.system_prompt,
                    json.dumps(trace.user_payload, ensure_ascii=False),
                    json.dumps(trace.raw_response, ensure_ascii=False),
                    json.dumps(trace.parsed_response, ensure_ascii=False),
                    trace.error_reason,
                    trace.error_message,
                    trace.finish_reason,
                    trace.latency_ms,
                    trace.prompt_tokens,
                    trace.completion_tokens,
                    trace.total_tokens,
                    trace.usage_source,
                    json.dumps(trace.usage_details, ensure_ascii=False),
                    trace.created_at.isoformat(),
                ),
            )

    def save_subagent_run(self, *, request: Any, budget: Any, status: str, result: Any | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        result_payload = result.to_dict() if result is not None else {}
        usage = result_payload.get('usage', {}) if isinstance(result_payload, dict) else {}
        tool_history = result_payload.get('tool_history', []) if isinstance(result_payload, dict) else []
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO subagent_runs (
                    subagent_id, parent_run_id, attempt, role, competitor, field_name, objective,
                    seed_queries_json, budget_json, status, result_json, tool_history_json,
                    prompt_tokens, completion_tokens, total_tokens, latency_ms, error_message,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subagent_id) DO UPDATE SET
                    status = excluded.status,
                    result_json = excluded.result_json,
                    tool_history_json = excluded.tool_history_json,
                    prompt_tokens = excluded.prompt_tokens,
                    completion_tokens = excluded.completion_tokens,
                    total_tokens = excluded.total_tokens,
                    latency_ms = excluded.latency_ms,
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
                ''',
                (
                    request.subagent_id,
                    request.parent_run_id,
                    request.attempt,
                    'collector.deep_dive',
                    request.competitor,
                    request.field_name,
                    request.objective,
                    json.dumps(request.seed_queries, ensure_ascii=False),
                    json.dumps(asdict(budget), ensure_ascii=False),
                    status,
                    json.dumps(result_payload, ensure_ascii=False),
                    json.dumps(tool_history, ensure_ascii=False),
                    int(usage.get('prompt_tokens', 0) or 0),
                    int(usage.get('completion_tokens', 0) or 0),
                    int(usage.get('total_tokens', 0) or 0),
                    int(usage.get('latency_ms', 0) or 0),
                    str(result_payload.get('error', '') or ''),
                    now,
                    now,
                ),
            )

    def list_subagent_runs(self, parent_run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT * FROM subagent_runs WHERE parent_run_id = ? ORDER BY created_at ASC',
                (parent_run_id,),
            ).fetchall()
        return [
            {
                'subagent_id': row['subagent_id'],
                'parent_run_id': row['parent_run_id'],
                'attempt': row['attempt'],
                'role': row['role'],
                'competitor': row['competitor'],
                'field_name': row['field_name'],
                'objective': row['objective'],
                'seed_queries': json.loads(row['seed_queries_json']),
                'budget': json.loads(row['budget_json']),
                'status': row['status'],
                'result': json.loads(row['result_json']),
                'tool_history': json.loads(row['tool_history_json']),
                'prompt_tokens': row['prompt_tokens'],
                'completion_tokens': row['completion_tokens'],
                'total_tokens': row['total_tokens'],
                'latency_ms': row['latency_ms'],
                'error_message': row['error_message'],
                'created_at': row['created_at'],
                'updated_at': row['updated_at'],
            }
            for row in rows
        ]

    def list_llm_calls(
        self,
        run_id: str,
        *,
        node_name: str | None = None,
        attempt: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = 'SELECT * FROM llm_calls WHERE run_id = ?'
        params: list[Any] = [run_id]
        if node_name is not None:
            sql += ' AND node_name = ?'
            params.append(node_name)
        if attempt is not None:
            sql += ' AND attempt = ?'
            params.append(attempt)
        sql += ' ORDER BY created_at ASC'
        with self._connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                'trace_id': row['trace_id'],
                'run_id': row['run_id'],
                'attempt': row['attempt'],
                'node_name': row['node_name'],
                'agent_name': row['agent_name'],
                'trace_name': row['trace_name'],
                'model': row['model'],
                'status': row['status'],
                'system_prompt': row['system_prompt'],
                'user_payload': json.loads(row['user_payload_json']),
                'raw_response': json.loads(row['raw_response_json']),
                'parsed_response': json.loads(row['parsed_response_json']),
                'error_reason': row['error_reason'],
                'error_message': row['error_message'],
                'finish_reason': row['finish_reason'],
                'latency_ms': row['latency_ms'],
                'prompt_tokens': row['prompt_tokens'],
                'completion_tokens': row['completion_tokens'],
                'total_tokens': row['total_tokens'],
                'usage_source': row['usage_source'],
                'usage_details': json.loads(row['usage_details_json']),
                'created_at': row['created_at'],
            }
            for row in rows
        ]

    def replay_node_io(self, run_id: str, node_name: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                '''
                SELECT io_type, payload_json, created_at
                FROM agent_io
                WHERE run_id = ? AND node_name = ?
                ORDER BY id ASC
                ''',
                (run_id, node_name),
            ).fetchall()
        return [
            {'io_type': row['io_type'], 'payload': json.loads(row['payload_json']), 'created_at': row['created_at']}
            for row in rows
        ]

    def audit_manual_intervention(
        self,
        *,
        run_id: str,
        node_name: str,
        action: str,
        before: dict[str, Any],
        after: dict[str, Any],
        reason: str,
        actor: str,
    ) -> None:
        now_s = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                '''
                INSERT INTO manual_interventions (run_id, node_name, action, before_json, after_json, reason, actor, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    run_id,
                    node_name,
                    action,
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                    reason,
                    actor,
                    now_s,
                ),
            )

    def list_manual_interventions(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                '''
                SELECT node_name, action, before_json, after_json, reason, actor, created_at
                FROM manual_interventions
                WHERE run_id = ?
                ORDER BY id ASC
                ''',
                (run_id,),
            ).fetchall()
        return [
            {
                'node_name': row['node_name'],
                'action': row['action'],
                'before': json.loads(row['before_json']),
                'after': json.loads(row['after_json']),
                'reason': row['reason'],
                'actor': row['actor'],
                'created_at': row['created_at'],
            }
            for row in rows
        ]

    def delete_run(self, run_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute('SELECT 1 FROM runs WHERE run_id = ?', (run_id,)).fetchone()
            if row is None:
                return False

            for table in (
                'events',
                'agent_runs',
                'agent_io',
                'run_checkpoints',
                'manual_interventions',
                'stage_handoffs',
                'llm_calls',
                'evidence_raw_contents',
                'run_comparison_corpus_links',
                'run_conversations',
                'conversation_messages',
                'conversation_turns',
                'conversation_memory',
                'report_revisions',
            ):
                conn.execute(f'DELETE FROM {table} WHERE run_id = ?', (run_id,))
            conn.execute('DELETE FROM subagent_runs WHERE parent_run_id = ?', (run_id,))
            conn.execute('DELETE FROM runs WHERE run_id = ?', (run_id,))
        if self.cache is not None:
            self.cache.delete_run(run_id)
        return True


SQLiteStore = PostgresStore
