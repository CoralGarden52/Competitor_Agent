from __future__ import annotations

from app.core.collector.deep_dive import CollectorDeepDiveCoordinator
from app.core.config import AppConfig
from app.core.models import AnalysisSchemaField
from harness.subagents import SubagentResult


class _FakeExecutor:
    def __init__(self) -> None:
        self.requests = []

    def run(self, *, request, role, budget):
        self.requests.append(request)
        return SubagentResult(
            subagent_id=request.subagent_id,
            status='completed',
            competitor=request.competitor,
            field_name=request.field_name,
            verification_claims=['confirmed'],
        )


def test_deep_dive_prioritizes_qa_and_skips_multi_host_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        'harness.subagents.tracing.get_tracing_runtime',
        lambda: type('Runtime', (), {'langsmith_enabled': False, 'client': None})(),
    )
    executor = _FakeExecutor()
    config = AppConfig(
        subagent_enabled=True,
        subagent_max_tasks_per_collect=12,
        subagent_max_concurrency=1,
    )
    coordinator = CollectorDeepDiveCoordinator(executor=executor, config=config)
    evidences = [
        {'competitor': 'alpha', 'schema_field': 'feature_tree', 'source_url': 'https://one.example/a'},
        {'competitor': 'alpha', 'schema_field': 'feature_tree', 'source_url': 'https://two.example/b'},
        {'competitor': 'alpha', 'schema_field': 'pricing_model', 'source_url': 'https://one.example/pricing'},
    ]
    result = coordinator.enrich(
        run_id='preview',
        attempt=0,
        industry='saas',
        competitors=['alpha'],
        schema_plan=[
            AnalysisSchemaField(field_name='feature_tree', priority=1),
            AnalysisSchemaField(field_name='pricing_model', priority=2),
        ],
        evidences=evidences,
        field_query_overrides={'alpha::feature_tree': ['alpha capabilities', 'alpha docs']},
    )

    assert [item.field_name for item in executor.requests] == ['feature_tree']
    assert result.evidences[0]['verification_status'] == 'supported'
    assert result.evidences[-1]['source_host_count'] == 1


def test_deep_dive_caps_generated_tasks(monkeypatch) -> None:
    monkeypatch.setattr(
        'harness.subagents.tracing.get_tracing_runtime',
        lambda: type('Runtime', (), {'langsmith_enabled': False, 'client': None})(),
    )
    executor = _FakeExecutor()
    coordinator = CollectorDeepDiveCoordinator(
        executor=executor,
        config=AppConfig(subagent_enabled=True, subagent_max_tasks_per_collect=2, subagent_max_concurrency=1),
    )
    coordinator.enrich(
        run_id='preview',
        attempt=0,
        industry='saas',
        competitors=['alpha'],
        schema_plan=[
            AnalysisSchemaField(field_name='one', priority=1),
            AnalysisSchemaField(field_name='two', priority=2),
            AnalysisSchemaField(field_name='three', priority=3),
        ],
        evidences=[],
    )
    assert len(executor.requests) == 2
