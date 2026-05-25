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


def test_generate_search_queries_uses_generic_product_templates_for_category_prompt() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    queries = planner._generate_search_queries('线上会议软件竞品分析', [], industry='')
    assert queries[:4] == ['线上会议软件 官网', '线上会议软件 产品', '线上会议软件 竞品', '线上会议软件 替代品']
    assert len(queries) == 4


def test_build_expansion_queries_uses_hints_and_candidate_pool() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    queries = planner._build_expansion_queries(competitor_hints=['飞书'], candidate_pool=['腾讯会议', '钉钉'])  # type: ignore[attr-defined]
    assert '飞书 替代品' in queries
    assert '腾讯会议 竞品' in queries
    assert len(queries) == 4


def test_discover_competitors_grouped_filters_names_outside_candidate_pool() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    planner._generate_search_queries = lambda *_args, **_kwargs: ['ai assistant competitors']  # type: ignore[method-assign]
    planner._search_and_summarize = lambda *_args, **_kwargs: [  # type: ignore[method-assign]
        {
            'title': 'OpenAI - AI assistant platform',
            'url': 'https://openai.com/chatgpt',
            'summary': 'OpenAI provides ChatGPT and enterprise AI assistant workflows.',
        }
    ]
    planner._chat_json = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        'direct': [
            {'name': 'ImaginaryAI', 'reason': 'hallucinated name', 'confidence': 0.9},
            {'name': 'OpenAI', 'reason': 'seen in search results', 'confidence': 0.8},
        ],
        'substitute': [],
    }
    result = planner.discover_competitors_grouped(prompt='AI assistant', industry='saas', competitor_hints=[])
    direct_names = [item['name'] for item in result['competitors']['direct']]
    assert 'OpenAI' in result['candidate_pool']
    assert direct_names == ['OpenAI']


def test_build_candidate_pool_extracts_names_from_search_snippets() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    pool = planner._build_candidate_pool(  # type: ignore[attr-defined]
        prompt='协作办公软件产品',
        industry='',
        competitor_hints=[],
        search_results=[
            {
                'title': '有哪些优质的协同办公类SaaS产品？',
                'url': 'https://example.com/article',
                'summary': (
                    '三、钉钉 钉钉是阿里巴巴集团推出的免费沟通与协同多端平台。'
                    '四、语雀 语雀孵化自蚂蚁集团。'
                    '五、飞书 飞书是字节跳动推出的一款协同办公软件。'
                    '六、Trello Trello是一款简单易用的团队协作工具。'
                    '七、Asana Asana是一款强大的项目管理工具。'
                ),
            }
        ],
    )
    assert '钉钉' in pool
    assert '语雀' in pool
    assert '飞书' in pool
    assert 'Trello' in pool
    assert 'Asana' in pool
    assert '协同办公类SaaS产品' not in pool


def test_build_candidate_pool_prefers_llm_entity_extraction() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    planner._chat_json = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        'candidate_names': ['腾讯会议', '飞书会议', 'Google Meet', '以下']
    }
    pool = planner._build_candidate_pool(  # type: ignore[attr-defined]
        prompt='线上会议软件竞品分析',
        industry='',
        competitor_hints=[],
        search_results=[
            {
                'title': '在线会议软件',
                'url': 'https://example.com/a',
                'summary': '包括腾讯会议、飞书会议、Google Meet。',
            }
        ],
    )
    assert pool == ['腾讯会议', '飞书会议', 'Google Meet']


def test_build_candidate_pool_ranks_repeated_entities_higher() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    pool = planner._build_candidate_pool(  # type: ignore[attr-defined]
        prompt='线上会议软件竞品分析',
        industry='',
        competitor_hints=[],
        search_results=[
            {
                'title': '最佳6款Zoom替代软件及竞争对手',
                'url': 'https://example.com/alternatives',
                'summary': '包括 Zoom、腾讯会议、飞书会议、Google Meet 和 Teams。',
            },
            {
                'title': '2026年最佳20款在线会议软件：完整对比',
                'url': 'https://example.com/compare',
                'summary': '1、腾讯会议 2、飞书会议 3、Zoom 4、Google Meet。',
            },
            {
                'title': '腾讯会议官方',
                'url': 'https://meeting.tencent.com/',
                'summary': '腾讯会议提供在线会议、屏幕共享与协作能力。',
            },
        ],
    )
    assert pool[:4] == ['腾讯会议', 'Zoom', '飞书会议', 'Google Meet']


def test_normalize_dynamic_schema_repairs_generic_query_templates() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    plan = planner._normalize_dynamic_schema([  # type: ignore[attr-defined]
        {
            'field_name': 'deployment_model',
            'query_templates': ['deployment model'],
            'recommended_sources': ['official'],
            'priority': 1,
        }
    ])
    deployment = next(item for item in plan if item['field_name'] == 'deployment_model')
    assert len(deployment['query_templates']) >= 2
    assert all('{product}' in template for template in deployment['query_templates'])


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
