from __future__ import annotations

from app.agents.analyst_agent import AnalystAgent
from app.core.config import AppConfig
from app.core.models import AnalysisFieldResult, AnalysisSchemaField, RawEvidence, RunState


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
                'summary': '该产品提供会议纪要、实时转写和智能待办提取。',
                'normalized_value': {
                    'key_observations': ['会议纪要', '实时转写', '智能待办提取'],
                    'value': '原生 AI 会议能力较完整',
                },
                'evidence_gaps': ['缺少公开准确率指标'],
            }

    agent = AnalystAgent(llm=_DummyLLM(), store=_DummyStore())
    result = agent._analyze_single_field(
        competitor='腾讯会议',
        field_name='ai_meeting_native_features',
        evidences=[
            RawEvidence(
                source_url='https://example.com',
                snippet='支持 AI 会议纪要、实时转写、待办提取。',
                title='AI 会议能力',
                query='腾讯会议 AI 功能',
                source_type='official',
            )
        ],
        industry='meeting_software',
        schema_item=AnalysisSchemaField(
            field_name='ai_meeting_native_features',
            query_templates=['{product} AI会议能力', '{product} 实时转写 纪要'],
            recommended_sources=['官网', '产品更新日志'],
            priority=1,
        ),
    )

    assert captured['user_payload']['field_context']['query_templates'] == ['{product} AI会议能力', '{product} 实时转写 纪要']
    assert captured['user_payload']['field_context']['recommended_sources'] == ['官网', '产品更新日志']
    assert result.summary == '该产品提供会议纪要、实时转写和智能待办提取。'
    assert result.normalized_value['key_observations'] == ['会议纪要', '实时转写', '智能待办提取']
    assert result.evidence_gaps == ['缺少公开准确率指标']


def test_analyze_single_field_fallback_is_field_aware() -> None:
    class _FailingLLM:
        def invoke_json(self, **kwargs):
            raise RuntimeError('boom')

    agent = AnalystAgent(llm=_FailingLLM(), store=_DummyStore())
    result = agent._analyze_single_field(
        competitor='飞书',
        field_name='pricing_model',
        evidences=[
            RawEvidence(
                source_url='https://example.com/pricing',
                snippet='企业版按席位收费，提供按年订阅方案和免费基础版。',
                title='定价方案',
                query='飞书 价格 套餐',
                source_type='official',
            )
        ],
        industry='collaboration_software',
        schema_item=AnalysisSchemaField(
            field_name='pricing_model',
            query_templates=['{product} 价格 套餐', '{product} 企业版 计费'],
            recommended_sources=['官网', '定价页'],
            priority=1,
        ),
    )

    assert '定价模式' in result.summary or '定价' in result.summary
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
        competitors=['钉钉', '飞书'],
        analysis_schema_plan=[
            AnalysisSchemaField(field_name='feature_tree', priority=1),
            AnalysisSchemaField(field_name='pricing_model', priority=2),
        ],
        evidences=[
            RawEvidence(source_url='https://example.com/1', snippet='a', query='钉钉', domain_extensions={'schema_field': 'feature_tree'}),
            RawEvidence(source_url='https://example.com/2', snippet='b', query='钉钉', domain_extensions={'schema_field': 'pricing_model'}),
            RawEvidence(source_url='https://example.com/3', snippet='c', query='飞书', domain_extensions={'schema_field': 'feature_tree'}),
            RawEvidence(source_url='https://example.com/4', snippet='d', query='飞书', domain_extensions={'schema_field': 'pricing_model'}),
        ],
    )

    output = agent.run_llm(state)

    assert [record.product_name for record in output.competitors] == ['钉钉', '飞书']
    assert [field.field_name for field in output.competitors[0].fields] == ['feature_tree', 'pricing_model']
    assert output.competitors[0].fields[0].summary == '钉钉-feature_tree'
    assert output.competitors[1].fields[1].summary == '飞书-pricing_model'
