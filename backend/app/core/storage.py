from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.models import (
    ApprovalPolicy,
    AnalyzeHandoff,
    CollectHandoff,
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


class SQLiteStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

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
                CREATE INDEX IF NOT EXISTS idx_llm_calls_run_node_attempt
                ON llm_calls(run_id, node_name, attempt, created_at)
                '''
            )
            self._seed_default_schema_versions(conn)
            self._seed_default_policies(conn)
            self._seed_default_field_risks(conn)

    def _seed_default_schema_versions(self, conn: sqlite3.Connection) -> None:
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

    def _seed_default_policies(self, conn: sqlite3.Connection) -> None:
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

    def _seed_default_field_risks(self, conn: sqlite3.Connection) -> None:
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
            existing = conn.execute('SELECT run_id, created_at FROM runs WHERE run_id = ?', (state.run_id,)).fetchone()
            created_at = existing['created_at'] if existing else now
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
                    len(state.competitors),
                    state.model_dump_json(),
                    created_at,
                    now,
                ),
            )

    def append_event(self, event: EventRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                'INSERT INTO events (run_id, stage, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)',
                (
                    event.run_id,
                    event.stage.value,
                    event.event_type,
                    json.dumps(event.payload, ensure_ascii=False),
                    event.created_at.isoformat(),
                ),
            )

    def get_state(self, run_id: str) -> RunState | None:
        with self._connect() as conn:
            row = conn.execute('SELECT state_json FROM runs WHERE run_id = ?', (run_id,)).fetchone()
        if row is None:
            return None
        return RunState.model_validate_json(row['state_json'])

    def list_runs(self, limit: int = 20) -> list[RunSummary]:
        with self._connect() as conn:
            rows = conn.execute(
                'SELECT run_id, industry, status, competitor_count, created_at, updated_at FROM runs ORDER BY updated_at DESC LIMIT ?',
                (limit,),
            ).fetchall()
        return [
            RunSummary(
                run_id=row['run_id'],
                industry=row['industry'],
                status=row['status'],
                competitor_count=row['competitor_count'],
                created_at=datetime.fromisoformat(row['created_at']),
                updated_at=datetime.fromisoformat(row['updated_at']),
            )
            for row in rows
        ]

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
        return {
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
        handoff: PlanHandoff | CollectHandoff | AnalyzeHandoff,
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
    ) -> PlanHandoff | CollectHandoff | AnalyzeHandoff | None:
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
