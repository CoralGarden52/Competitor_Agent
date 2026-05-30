from __future__ import annotations

from app.agents.analyst_agent import AnalystAgent
from app.core.config import AppConfig
from app.core.models import AnalysisFieldResult, AnalysisSchemaField, CompetitorAnalysisRecord, RawEvidence, RunState


class _DummyStore:
    def get_active_domain_schema(self, industry: str):
        return {
            'industry': industry,
            'version': 'v1',
            'required_extension_fields': [],
        }


def test_analyze_single_field_uses_schema_context_and_normalized_value() -> None:
    captured: dict = {}

    class _DummyLLM:
        def invoke_json(self, **kwargs):
            captured.update(kwargs)
            return {
                'summary': 'Provides AI meeting notes and realtime transcription.',
                'normalized_value': {
                    'key_observations': ['meeting notes', 'realtime transcription'],
                    'value': 'Native AI meeting capability',
                },
                'evidence_gaps': ['missing public accuracy benchmark'],
            }

    agent = AnalystAgent(llm=_DummyLLM(), store=_DummyStore())
    result = agent._analyze_single_field(
        competitor='alpha',
        field_name='ai_meeting_native_features',
        evidences=[
            RawEvidence(
                source_url='https://example.com',
                snippet='Supports AI meeting notes and realtime transcription.',
                title='AI Meeting Features',
                query='alpha ai features',
                source_type='official',
            )
        ],
        industry='meeting_software',
        schema_item=AnalysisSchemaField(
            field_name='ai_meeting_native_features',
            query_templates=['{product} AI meeting features', '{product} realtime transcription notes'],
            recommended_sources=['official_site', 'release_notes'],
            priority=1,
        ),
    )

    assert captured['user_payload']['field_context']['query_templates'] == ['{product} AI meeting features', '{product} realtime transcription notes']
    assert captured['user_payload']['field_context']['recommended_sources'] == ['official_site', 'release_notes']
    assert result.summary == 'Provides AI meeting notes and realtime transcription.'
    assert result.normalized_value['key_observations'] == ['meeting notes', 'realtime transcription']
    assert result.evidence_gaps == ['missing public accuracy benchmark']


def test_analyze_single_field_fallback_is_field_aware() -> None:
    class _FailingLLM:
        def invoke_json(self, **kwargs):
            raise RuntimeError('boom')

    agent = AnalystAgent(llm=_FailingLLM(), store=_DummyStore())
    result = agent._analyze_single_field(
        competitor='alpha',
        field_name='pricing_model',
        evidences=[
            RawEvidence(
                source_url='https://example.com/pricing',
                snippet='Enterprise plan is billed per seat and has yearly subscription options.',
                title='Pricing',
                query='alpha pricing plans',
                source_type='official',
            )
        ],
        industry='collaboration_software',
        schema_item=AnalysisSchemaField(
            field_name='pricing_model',
            query_templates=['{product} pricing plans', '{product} enterprise billing'],
            recommended_sources=['official_site', 'pricing_page'],
            priority=1,
        ),
    )

    assert 'pricing' in result.summary.lower() or 'plan' in result.summary.lower()
    assert result.normalized_value['model_type'] in {'subscription', 'unknown'}
    assert isinstance(result.normalized_value['tiers'], list)


def test_run_llm_preserves_competitor_and_field_order() -> None:
    class _DummyLLM:
        config = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m', analyze_llm_max_workers=4)

    agent = AnalystAgent(llm=_DummyLLM(), store=_DummyStore())

    def _fake_analyze_single_field(*, competitor, field_name, evidences, industry, schema_item=None):
        return AnalysisFieldResult(
            field_name=field_name,
            summary=f'{competitor}-{field_name}',
            evidence_refs=[],
            confidence=0.7,
            normalized_value={},
            evidence_gaps=[],
        )

    agent._analyze_single_field = _fake_analyze_single_field  # type: ignore[method-assign]

    state = RunState(
        industry='collaboration_software',
        competitors=['alpha', 'beta'],
        analysis_schema_plan=[
            AnalysisSchemaField(field_name='feature_tree', priority=1),
            AnalysisSchemaField(field_name='pricing_model', priority=2),
        ],
        evidences=[
            RawEvidence(source_url='https://example.com/1', snippet='a', query='alpha', domain_extensions={'competitor': 'alpha', 'schema_field': 'feature_tree'}),
            RawEvidence(source_url='https://example.com/2', snippet='b', query='alpha', domain_extensions={'competitor': 'alpha', 'schema_field': 'pricing_model'}),
            RawEvidence(source_url='https://example.com/3', snippet='c', query='beta', domain_extensions={'competitor': 'beta', 'schema_field': 'feature_tree'}),
            RawEvidence(source_url='https://example.com/4', snippet='d', query='beta', domain_extensions={'competitor': 'beta', 'schema_field': 'pricing_model'}),
        ],
    )

    output = agent.run_llm(state)

    assert [record.product_name for record in output.competitors] == ['alpha', 'beta']
    assert [field.field_name for field in output.competitors[0].fields] == ['feature_tree', 'pricing_model']
    assert output.competitors[0].fields[0].summary == 'alpha-feature_tree'
    assert output.competitors[1].fields[1].summary == 'beta-pricing_model'


def test_run_llm_with_reanalyze_targets_reuses_previous_fields() -> None:
    class _DummyLLM:
        config = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m', analyze_llm_max_workers=4)

    agent = AnalystAgent(llm=_DummyLLM(), store=_DummyStore())

    def _fake_analyze_single_field(*, competitor, field_name, evidences, industry, schema_item=None):
        return AnalysisFieldResult(
            field_name=field_name,
            summary=f'NEW-{competitor}-{field_name}',
            evidence_refs=['ev-new'],
            confidence=0.9,
            normalized_value={'value': 'new'},
            evidence_gaps=[],
        )

    agent._analyze_single_field = _fake_analyze_single_field  # type: ignore[method-assign]

    state = RunState(
        industry='saas',
        competitors=['alpha', 'beta'],
        analysis_schema_plan=[
            AnalysisSchemaField(field_name='feature_tree', priority=1),
            AnalysisSchemaField(field_name='pricing_model', priority=2),
        ],
        evidences=[
            RawEvidence(source_url='https://example.com/1', snippet='a', query='alpha', domain_extensions={'competitor': 'alpha', 'schema_field': 'feature_tree'}),
            RawEvidence(source_url='https://example.com/2', snippet='b', query='alpha', domain_extensions={'competitor': 'alpha', 'schema_field': 'pricing_model'}),
            RawEvidence(source_url='https://example.com/3', snippet='c', query='beta', domain_extensions={'competitor': 'beta', 'schema_field': 'feature_tree'}),
            RawEvidence(source_url='https://example.com/4', snippet='d', query='beta', domain_extensions={'competitor': 'beta', 'schema_field': 'pricing_model'}),
        ],
    )

    previous_records = [
        CompetitorAnalysisRecord(
            product_name='alpha',
            fields=[
                AnalysisFieldResult(field_name='feature_tree', summary='OLD-alpha-feature_tree', evidence_refs=['ev1'], confidence=0.5, normalized_value={}, evidence_gaps=[]),
                AnalysisFieldResult(field_name='pricing_model', summary='OLD-alpha-pricing_model', evidence_refs=['ev2'], confidence=0.5, normalized_value={}, evidence_gaps=[]),
            ],
        ),
        CompetitorAnalysisRecord(
            product_name='beta',
            fields=[
                AnalysisFieldResult(field_name='feature_tree', summary='OLD-beta-feature_tree', evidence_refs=['ev3'], confidence=0.5, normalized_value={}, evidence_gaps=[]),
                AnalysisFieldResult(field_name='pricing_model', summary='OLD-beta-pricing_model', evidence_refs=['ev4'], confidence=0.5, normalized_value={}, evidence_gaps=[]),
            ],
        ),
    ]

    out = agent.run_llm(
        state,
        reanalyze_targets={'alpha': {'pricing_model'}},
        previous_records=previous_records,
    )

    by_comp = {r.product_name: r for r in out.competitors}
    alpha_fields = {f.field_name: f for f in by_comp['alpha'].fields}
    beta_fields = {f.field_name: f for f in by_comp['beta'].fields}
    assert alpha_fields['pricing_model'].summary.startswith('NEW-alpha-pricing_model')
    assert alpha_fields['feature_tree'].summary == 'OLD-alpha-feature_tree'
    assert beta_fields['feature_tree'].summary == 'OLD-beta-feature_tree'
    assert beta_fields['pricing_model'].summary == 'OLD-beta-pricing_model'


def test_run_llm_with_reanalyze_targets_missing_previous_field_fallback() -> None:
    class _DummyLLM:
        config = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m', analyze_llm_max_workers=4)

    agent = AnalystAgent(llm=_DummyLLM(), store=_DummyStore())

    def _fake_analyze_single_field(*, competitor, field_name, evidences, industry, schema_item=None):
        return AnalysisFieldResult(
            field_name=field_name,
            summary=f'NEW-{competitor}-{field_name}',
            evidence_refs=['ev-new'],
            confidence=0.9,
            normalized_value={'value': 'new'},
            evidence_gaps=[],
        )

    agent._analyze_single_field = _fake_analyze_single_field  # type: ignore[method-assign]

    state = RunState(
        industry='saas',
        competitors=['alpha'],
        analysis_schema_plan=[
            AnalysisSchemaField(field_name='feature_tree', priority=1),
            AnalysisSchemaField(field_name='pricing_model', priority=2),
        ],
        evidences=[
            RawEvidence(source_url='https://example.com/1', snippet='alpha feature text', query='alpha feature', domain_extensions={'competitor': 'alpha', 'schema_field': 'feature_tree'}),
            RawEvidence(source_url='https://example.com/2', snippet='alpha pricing text', query='alpha pricing', domain_extensions={'competitor': 'alpha', 'schema_field': 'pricing_model'}),
        ],
    )

    previous_records = [
        CompetitorAnalysisRecord(
            product_name='alpha',
            fields=[
                AnalysisFieldResult(field_name='feature_tree', summary='OLD-alpha-feature_tree', evidence_refs=['ev1'], confidence=0.5, normalized_value={}, evidence_gaps=[]),
            ],
        )
    ]

    out = agent.run_llm(
        state,
        reanalyze_targets={'alpha': {'pricing_model'}},
        previous_records=previous_records,
    )

    fields = {f.field_name: f for f in out.competitors[0].fields}
    assert fields['pricing_model'].summary.startswith('NEW-alpha-pricing_model')
    assert fields['feature_tree'].summary == 'OLD-alpha-feature_tree'
