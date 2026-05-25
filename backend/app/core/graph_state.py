from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import NotRequired, TypedDict

from app.core.models import AnalyzeOutput, CollectOutput, DraftOutput, QAOutput, RunRequest, StageName, StageSnapshot


class WorkflowGraphState(TypedDict):
    run_id: str
    attempt: int
    parent_attempt: int | None
    status: str
    current_stage: str
    industry: str
    competitors: list[str]
    user_prompt: str
    language: str
    timeframe: str
    raw_evidences: list[dict]
    competitor_analyses: list[dict]
    profiles: list[dict]
    findings: list[dict]
    report: dict | None
    tickets: list[dict]
    core_schema_version: str
    domain_schema_version: str
    self_eval: dict
    policy_decisions: list[dict]
    stage_events: list[dict]
    errors: list[str]
    ticket_id: NotRequired[str | None]


def _hash_payload(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8', errors='ignore')
    return hashlib.sha256(encoded).hexdigest()


def make_stage_snapshot(*, run_id: str, stage: StageName, input_payload: dict, output_payload: dict) -> StageSnapshot:
    return StageSnapshot(
        run_id=run_id,
        stage=stage,
        input_hash=_hash_payload(input_payload),
        output_hash=_hash_payload(output_payload),
        created_at=datetime.now(UTC),
    )


def init_graph_state_from_run_request(*, request: RunRequest, run_id: str, core_schema_version: str, domain_schema_version: str) -> WorkflowGraphState:
    return {
        'run_id': run_id,
        'attempt': 1,
        'parent_attempt': None,
        'status': 'running',
        'current_stage': StageName.plan.value,
        'industry': request.industry.strip().lower(),
        'competitors': request.competitors,
        'user_prompt': request.user_prompt.strip(),
        'language': request.language,
        'timeframe': request.timeframe,
        'raw_evidences': [],
        'competitor_analyses': [],
        'profiles': [],
        'findings': [],
        'report': None,
        'tickets': [],
        'core_schema_version': core_schema_version,
        'domain_schema_version': domain_schema_version,
        'self_eval': {},
        'policy_decisions': [],
        'stage_events': [],
        'errors': [],
    }


def merge_collect_output(state: WorkflowGraphState, output: CollectOutput) -> WorkflowGraphState:
    merged = dict(state)
    merged['raw_evidences'] = [item.model_dump() for item in output.raw_evidences]
    merged['errors'] = [*state['errors'], *output.errors]
    merged['stage_events'] = [*state['stage_events'], *output.provider_events]
    merged['current_stage'] = StageName.collect.value
    return merged


def merge_analyze_output(state: WorkflowGraphState, output: AnalyzeOutput) -> WorkflowGraphState:
    merged = dict(state)
    merged['competitor_analyses'] = [item.model_dump() for item in output.competitors]
    merged['profiles'] = [item.model_dump() for item in output.profiles]
    merged['findings'] = [item.model_dump() for item in output.findings]
    merged['current_stage'] = StageName.analyze.value
    return merged


def merge_draft_output(state: WorkflowGraphState, output: DraftOutput) -> WorkflowGraphState:
    merged = dict(state)
    merged['report'] = output.report.model_dump()
    merged['current_stage'] = StageName.draft.value
    return merged


def merge_qa_output(state: WorkflowGraphState, output: QAOutput) -> WorkflowGraphState:
    merged = dict(state)
    merged['current_stage'] = StageName.qa.value
    if output.ticket is not None:
        merged['tickets'] = [*state['tickets'], output.ticket.model_dump()]
    if output.issues:
        merged['errors'] = [*state['errors'], *[issue.message for issue in output.issues]]
    return merged
