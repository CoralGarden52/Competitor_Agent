from __future__ import annotations

from app.core.config import AppConfig
from app.core.planner_llm import CORE_DYNAMIC_FIELDS, PlannerLLMClient


def _assert_core_schema(plan: list[dict]) -> None:
    fields = [str(item.get('field_name', '')) for item in plan]
    assert set(fields) == set(CORE_DYNAMIC_FIELDS)
    for item in plan:
        templates = item.get('query_templates', [])
        assert isinstance(templates, list)
        assert len(templates) >= 2
        assert all(str(x).strip() for x in templates)
    user_feedback = next(x for x in plan if x.get('field_name') == 'user_feedback')
    joined = ' '.join(str(x) for x in user_feedback.get('query_templates', [])).lower()
    assert ('zhihu' in joined) or ('知乎' in joined)


def test_plan_schema_fallback_when_llm_disabled() -> None:
    cfg = AppConfig(openai_api_key='', openai_base_url='', openai_model='')
    planner = PlannerLLMClient(cfg)
    plan = planner.plan_schema(industry='general', target_product='agent', competitors=['a', 'b'])
    _assert_core_schema(plan)


def test_plan_schema_fallback_when_llm_raises() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    planner._chat_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('boom'))  # type: ignore[method-assign]
    plan = planner.plan_schema(industry='general', target_product='agent', competitors=['a'])
    _assert_core_schema(plan)


def test_plan_dynamic_schema_fallback_when_llm_disabled() -> None:
    cfg = AppConfig(openai_api_key='', openai_base_url='', openai_model='')
    planner = PlannerLLMClient(cfg)
    plan = planner.plan_dynamic_schema(prompt='general AI agent competitor analysis', industry='general', candidates=['OpenAI'])
    _assert_core_schema(plan)
    priorities = [int(item.get('priority', 0)) for item in plan]
    assert priorities == list(range(1, len(priorities) + 1))


def test_parse_json_content_plain_json() -> None:
    parsed = PlannerLLMClient._parse_json_content('{"ok": true, "k": "v"}')
    assert parsed['ok'] is True
    assert parsed['k'] == 'v'


def test_parse_json_content_with_fence() -> None:
    parsed = PlannerLLMClient._parse_json_content('```json\n{"ok": true}\n```')
    assert parsed['ok'] is True


def test_parse_json_content_invalid_text() -> None:
    try:
        PlannerLLMClient._parse_json_content('hello world')
        assert False, 'expected ValueError'
    except ValueError as exc:
        assert 'parse' in str(exc).lower()


def test_check_health_reports_parse_failure() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    planner._chat_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('llm_chat_failed: json_parse_failed: bad'))  # type: ignore[method-assign]
    health = planner.check_health()
    assert health['enabled'] is True
    assert health['success'] is False
    assert 'json_parse_failed' in str(health['reason'])
