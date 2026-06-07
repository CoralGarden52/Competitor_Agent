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


def test_generate_search_queries_uses_recent_comparison_templates_when_llm_fails() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    planner._chat_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('boom'))  # type: ignore[method-assign]
    queries = planner._generate_search_queries('线上会议软件竞品分析', [], industry='')
    assert queries[:4] == [
        '线上会议软件 近一年 对比',
        '线上会议软件 近一年 替代方案',
        '线上会议软件 近一年 排行榜',
    ]
    assert len(queries) == 3
    assert planner._last_comparison_search_plan['source'] == 'rule_fallback'  # type: ignore[attr-defined]
    assert planner._last_comparison_search_plan['strategy'] == 'industry_recent_comparison_corpus'  # type: ignore[attr-defined]


def test_generate_search_queries_prefers_llm_comparison_search_plan() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    planner._chat_json = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        'primary_query': '近一年主流会议软件对比',
        'expansion_queries': ['企业视频会议软件排行榜', '主流在线会议软件盘点'],
        'topic_key': 'meeting_software',
        'keywords': ['会议软件', '视频会议'],
    }
    queries = planner._generate_search_queries('请进行主流的会议软件竞品分析', [], industry='')
    assert queries == [
        '近一年主流会议软件对比',
        '企业视频会议软件排行榜 近一年',
        '主流在线会议软件盘点 近一年',
    ]
    assert planner._last_comparison_search_plan['source'] == 'llm'  # type: ignore[attr-defined]
    assert planner._last_comparison_search_plan['strategy'] == 'industry_recent_comparison_corpus'  # type: ignore[attr-defined]


def test_generate_search_queries_uses_industry_anchor_not_target_product_name() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)

    captured: dict[str, str] = {}

    def _fake_chat(_system_prompt: str, user_prompt: str, **_kwargs) -> dict[str, object]:
        captured['user_prompt'] = user_prompt
        return {
            'primary_query': '智能客服 SaaS 近一年 对比',
            'expansion_queries': ['智能客服 SaaS 主流产品 近一年'],
            'topic_key': 'customer_service_saas',
            'keywords': ['智能客服 SaaS'],
        }

    planner._chat_json = _fake_chat  # type: ignore[method-assign]
    queries = planner._generate_search_queries(
        '请分析纷享销客AI客服的竞品情况',
        ['Zendesk'],
        industry='智能客服 SaaS',
        product_profile={'product_category': 'AI客服'},
    )

    assert queries[0] == '智能客服 SaaS 近一年 对比'
    assert '行业搜索锚点：智能客服 SaaS' in captured['user_prompt']
    assert '不要把搜索目标放在单个产品的信息收集上' in captured['user_prompt']


def test_build_expansion_queries_stays_on_industry_level() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    queries = planner._build_expansion_queries(  # type: ignore[attr-defined]
        prompt='线上会议软件竞品分析',
        industry='',
        competitor_hints=['飞书'],
        candidate_pool=['腾讯会议', '钉钉'],
    )
    assert '线上会议软件 近一年 主流产品' in queries
    assert '线上会议软件 近一年 市场格局' in queries
    assert all('飞书' not in query and '腾讯会议' not in query for query in queries)
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
    planner._build_candidate_pool = lambda **_kwargs: ['OpenAI']  # type: ignore[method-assign]
    planner._collect_comparison_corpus = lambda **_kwargs: []  # type: ignore[method-assign]
    planner._synthesize_comparison_corpus = lambda **_kwargs: {  # type: ignore[method-assign]
        'direct': [],
        'substitute': [],
        'extra_schema_fields': [],
        'decision_evidence_refs': [],
    }
    planner._discover_from_search_results = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        'direct': planner._clean_candidates(  # type: ignore[attr-defined]
            [
                {'name': 'ImaginaryAI', 'reason': 'hallucinated name', 'confidence': 0.9},
                {'name': 'OpenAI', 'reason': 'seen in search results', 'confidence': 0.8},
            ],
            fallback_hints=[],
            default_fit='direct',
            allowed_names=['OpenAI'],
        ),
        'substitute': [],
    }
    result = planner.discover_competitors_grouped(prompt='AI assistant', industry='saas', competitor_hints=[])
    direct_names = [item['name'] for item in result['competitors']['direct']]
    assert 'OpenAI' in result['candidate_pool']
    assert direct_names == ['OpenAI']


def test_discover_competitors_grouped_prefers_comparison_corpus_over_search_result_name_extraction() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    planner._generate_search_queries = lambda *_args, **_kwargs: ['meeting software recent comparison']  # type: ignore[method-assign]
    planner._search_and_summarize = lambda *_args, **_kwargs: [  # type: ignore[method-assign]
        {'title': '2026 meeting software comparison', 'url': 'https://example.com/a', 'summary': 'Zoom vs Teams'}
    ]
    planner._build_candidate_pool = lambda **_kwargs: ['Zoom', 'SnippetNameB']  # type: ignore[method-assign]
    planner._collect_comparison_corpus = lambda **_kwargs: [  # type: ignore[method-assign]
        {'corpus_id': 'corpus_1', 'llm_extract': {'mentioned_competitors': ['Zoom'], 'comparison_dimensions': ['feature_tree']}}
    ]
    planner._synthesize_comparison_corpus = lambda **_kwargs: {  # type: ignore[method-assign]
        'direct': [{'name': 'Zoom', 'reason': 'corpus', 'confidence': 0.8, 'corpus_refs': ['corpus_1']}],
        'substitute': [],
        'extra_schema_fields': [],
        'decision_evidence_refs': ['corpus_1'],
    }
    planner._discover_from_search_results = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        'direct': [{'name': 'WrongSnippetCompetitor', 'reason': 'search', 'confidence': 0.7}],
        'substitute': [],
    }

    result = planner.discover_competitors_grouped(prompt='腾讯会议竞品分析', industry='video meeting saas', competitor_hints=[])

    assert [item['name'] for item in result['competitors']['direct']] == ['Zoom']
    assert [item['name'] for item in result['comparison_decision']['direct']] == ['Zoom']


def test_discover_competitors_grouped_does_not_fallback_when_corpus_synthesis_is_empty() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    planner._generate_search_queries = lambda *_args, **_kwargs: ['meeting software recent comparison']  # type: ignore[method-assign]
    planner._search_and_summarize = lambda *_args, **_kwargs: [  # type: ignore[method-assign]
        {'title': '2026 meeting software comparison', 'url': 'https://example.com/a', 'summary': 'Zoom vs Teams'}
    ]
    planner._build_candidate_pool = lambda **_kwargs: ['Zoom', 'Teams']  # type: ignore[method-assign]
    planner._collect_comparison_corpus = lambda **_kwargs: [  # type: ignore[method-assign]
        {'corpus_id': 'corpus_1', 'llm_extract': {'mentioned_competitors': ['Zoom'], 'comparison_dimensions': ['feature_tree']}}
    ]
    planner._synthesize_comparison_corpus = lambda **_kwargs: {  # type: ignore[method-assign]
        'direct': [],
        'substitute': [],
        'extra_schema_fields': [],
        'decision_evidence_refs': ['corpus_1'],
    }

    def _unexpected_discover(*_args, **_kwargs):
        raise AssertionError('search-result fallback should not run when comparison corpus already exists')

    planner._discover_from_search_results = _unexpected_discover  # type: ignore[method-assign]

    result = planner.discover_competitors_grouped(prompt='腾讯会议竞品分析', industry='video meeting saas', competitor_hints=[])

    assert result['competitors'] == {'direct': [], 'substitute': []}
    assert result['comparison_decision']['decision_evidence_refs'] == ['corpus_1']


def test_fallback_synthesize_comparison_corpus_does_not_emit_placeholder_dimensions() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)

    result = planner._fallback_synthesize_comparison_corpus(  # type: ignore[attr-defined]
        competitor_hints=[],
        candidate_pool=[],
        comparison_corpus=[],
    )

    assert result['extra_schema_fields'] == []


def test_normalize_synthesis_result_does_not_backfill_placeholder_dimensions() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)

    result = planner._normalize_synthesis_result(  # type: ignore[attr-defined]
        {
            'direct': [],
            'substitute': [],
            'extra_schema_fields': [{'field_name': 'feature_comparison', 'query_templates': ['{product} 功能对比'], 'recommended_sources': ['public_web'], 'priority': 6, 'corpus_refs': ['corpus_1']}],
            'decision_evidence_refs': ['corpus_1'],
        },
        candidate_pool=[],
    )

    assert [item['field_name'] for item in result['extra_schema_fields']] == ['feature_comparison']


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


def test_normalize_dynamic_schema_preserves_comparison_corpus_refs() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    plan = planner._normalize_dynamic_schema([  # type: ignore[attr-defined]
        {
            'field_name': 'deployment_model',
            'query_templates': ['{product} deployment'],
            'recommended_sources': ['official'],
            'priority': 1,
            'corpus_refs': ['corpus_a'],
        }
    ])
    deployment = next(item for item in plan if item['field_name'] == 'deployment_model')
    assert deployment['corpus_refs'] == ['corpus_a']


def test_normalize_dynamic_schema_never_trims_core_fields() -> None:
    cfg = AppConfig(openai_api_key='k', openai_base_url='https://example.com/v1', openai_model='m')
    planner = PlannerLLMClient(cfg)
    raw_plan = [
        {
            'field_name': f'dynamic_field_{index}',
            'query_templates': [f'{{product}} dynamic field {index}'],
            'recommended_sources': ['public_web'],
            'priority': index,
        }
        for index in range(1, 15)
    ]

    plan = planner._normalize_dynamic_schema(raw_plan)  # type: ignore[attr-defined]
    fields = [item['field_name'] for item in plan]

    assert len(plan) == 12
    assert fields[: len(CORE_DYNAMIC_FIELDS)] == CORE_DYNAMIC_FIELDS
    assert set(CORE_DYNAMIC_FIELDS).issubset(set(fields))
    assert 'pricing_model' in fields
    assert len([field for field in fields if field.startswith('dynamic_field_')]) == 7


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
