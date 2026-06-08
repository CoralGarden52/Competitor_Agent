from __future__ import annotations

import json
import re
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.deps import get_service
from app.core.models import EventRecord, QuestionnaireDesign, Report, RunState, StageName
from app.core.config import get_config
from app.core.wjx_export import WjxCliError
from app.main import create_app


def test_healthz() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get('/healthz')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


def test_runs_summary_endpoint() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.post('/runs/summary', json={'text': '分析在线会议软件市场竞争格局'})
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body.get('summary_text', ''), str)
    assert body['summary_text'].strip()


def test_runs_summary_endpoint_empty_text_422() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.post('/runs/summary', json={'text': ''})
    assert response.status_code == 422


def test_run_prompt_only_payload() -> None:
    app = create_app()
    client = TestClient(app)
    payload = {
        'industry': '',
        'competitors': [],
        'user_prompt': '对在线会议软件进行竞品分析',
        'language': 'zh-CN',
        'timeframe': 'last_12_months',
    }
    response = client.post('/runs', json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body['summary']['run_id']
    assert body['state']['status'] == 'running'


def test_run_happy_path() -> None:
    app = create_app()
    client = TestClient(app)
    payload = {
        'industry': 'saas',
        'competitors': ['alpha', 'beta'],
        'language': 'zh-CN',
        'timeframe': 'last_12_months',
    }
    response = client.post('/runs', json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body['state']['status'] == 'running'
    run_id = body['state']['run_id']

    final_body = body
    for _ in range(120):
        final_body = client.get(f'/runs/{run_id}').json()
        report = final_body['state'].get('report') or {}
        if final_body['state']['status'] in ('completed', 'failed') or str(report.get('markdown', '')).strip():
            break
        time.sleep(0.05)

    assert final_body['state']['status'] in ('completed', 'failed')
    assert len(final_body['state']['profiles']) >= 1

    profile = final_body['state']['profiles'][0]
    assert 'feature_tree' in profile
    assert 'advantages' in profile
    assert 'disadvantages' in profile
    assert 'pricing_model' in profile
    assert 'user_feedback' in profile


def test_replay_endpoint_includes_handoffs() -> None:
    app = create_app()
    client = TestClient(app)
    payload = {
        'industry': 'saas',
        'competitors': ['alpha'],
        'language': 'zh-CN',
        'timeframe': 'last_12_months',
    }
    create_resp = client.post('/runs', json=payload)
    assert create_resp.status_code == 200
    run_id = create_resp.json()['state']['run_id']

    for _ in range(40):
        state_resp = client.get(f'/runs/{run_id}')
        assert state_resp.status_code == 200
        if state_resp.json()['state']['status'] in ('completed', 'failed'):
            break
        time.sleep(0.05)

    replay_resp = client.get(f'/runs/{run_id}/replay')
    assert replay_resp.status_code == 200
    body = replay_resp.json()
    assert isinstance(body['handoffs'], list)
    assert isinstance(body.get('tool_events', []), list)
    assert any(item['handoff_type'] == 'PlanHandoff' for item in body['handoffs'])

    node_resp = client.get(f'/runs/{run_id}/nodes/analyze')
    assert node_resp.status_code == 200
    node_body = node_resp.json()
    assert isinstance(node_body['handoffs'], list)


def test_questionnaire_endpoint_from_report() -> None:
    app = create_app()
    client = TestClient(app)
    payload = {
        'industry': 'saas',
        'competitors': ['alpha'],
        'language': 'zh-CN',
        'timeframe': 'last_12_months',
    }
    create_resp = client.post('/runs', json=payload)
    assert create_resp.status_code == 200
    run_id = create_resp.json()['state']['run_id']

    final_body = create_resp.json()
    for _ in range(40):
        final_body = client.get(f'/runs/{run_id}').json()
        if final_body['state']['status'] in ('completed', 'failed'):
            break
        time.sleep(0.05)

    questionnaire_resp = client.post(
        f'/runs/{run_id}/questionnaire',
        json={
            'target_audience': 'AI 产品潜在购买者',
            'objective': '验证竞品差异与购买决策因素',
        },
    )
    assert questionnaire_resp.status_code == 200
    body = questionnaire_resp.json()
    assert body['title'].strip()
    assert body['target_audience'] == 'AI 产品潜在购买者'
    assert body['objective'] == '验证竞品差异与购买决策因素'
    assert isinstance(body['sections'], list)
    assert len(body['sections']) == 4
    assert body['markdown'].strip()


def test_update_report_markdown_endpoint_persists() -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        report=Report(executive_summary='old summary', markdown='# Old report', html='<h1>Old report</h1>'),
        status='completed',
    )
    service.store.save_state(state)

    response = client.patch(f'/runs/{state.run_id}/report', json={'markdown': '# Updated report\n\nnew body'})

    assert response.status_code == 200
    body = response.json()
    assert body['state']['report']['markdown'] == '# Updated report\n\nnew body'
    assert body['state']['report']['html'] == ''

    get_response = client.get(f'/runs/{state.run_id}')
    assert get_response.status_code == 200
    assert get_response.json()['state']['report']['markdown'] == '# Updated report\n\nnew body'


def test_update_report_markdown_endpoint_empty_markdown_422() -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        report=Report(executive_summary='old summary', markdown='# Old report'),
        status='completed',
    )
    service.store.save_state(state)

    response = client.patch(f'/runs/{state.run_id}/report', json={'markdown': ''})

    assert response.status_code == 422


def test_update_report_markdown_endpoint_404_for_missing_run() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.patch('/runs/run_missing_report_update/report', json={'markdown': '# Updated'})

    assert response.status_code == 404


def test_update_report_markdown_endpoint_404_for_missing_report() -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()
    state = RunState(industry='saas', competitors=['alpha'], status='completed')
    service.store.save_state(state)

    response = client.patch(f'/runs/{state.run_id}/report', json={'markdown': '# Updated'})

    assert response.status_code == 404


def test_questionnaire_endpoint_persists_design_to_run_and_workspace() -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        report=Report(executive_summary='report summary', markdown='# Report\n\nbody'),
        status='completed',
    )
    service.store.save_state(state)

    original_run_llm = service.questionnaire_agent.run_llm

    def fake_run_llm(*args, **kwargs) -> QuestionnaireDesign:  # noqa: ANN002, ANN003
        return QuestionnaireDesign(
            title='Saved Questionnaire',
            target_audience=kwargs.get('target_audience', ''),
            objective=kwargs.get('objective', ''),
            introduction='intro',
            markdown='# Saved Questionnaire\n\n1. question',
        )

    service.questionnaire_agent.run_llm = fake_run_llm
    try:
        response = client.post(
            f'/runs/{state.run_id}/questionnaire',
            json={'target_audience': 'users', 'objective': 'learn'},
        )
    finally:
        service.questionnaire_agent.run_llm = original_run_llm

    assert response.status_code == 200
    assert response.json()['markdown'] == '# Saved Questionnaire\n\n1. question'

    get_response = client.get(f'/runs/{state.run_id}')
    assert get_response.status_code == 200
    assert get_response.json()['state']['questionnaire']['markdown'] == '# Saved Questionnaire\n\n1. question'

    workspace_response = client.get(f'/runs/{state.run_id}/workspace')
    assert workspace_response.status_code == 200
    assert workspace_response.json()['questionnaire']['title'] == 'Saved Questionnaire'
    assert workspace_response.json()['questionnaire']['markdown'] == '# Saved Questionnaire\n\n1. question'


def test_update_questionnaire_markdown_endpoint_persists() -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        questionnaire=QuestionnaireDesign(
            title='Old Questionnaire',
            target_audience='users',
            objective='learn',
            introduction='intro',
            markdown='# Old Questionnaire\n\nbody',
        ),
        status='completed',
    )
    service.store.save_state(state)

    response = client.patch(f'/runs/{state.run_id}/questionnaire', json={'markdown': '# Updated Questionnaire\n\nnew body'})

    assert response.status_code == 200
    body = response.json()
    assert body['title'] == 'Updated Questionnaire'
    assert body['markdown'] == '# Updated Questionnaire\n\nnew body'

    get_response = client.get(f'/runs/{state.run_id}')
    assert get_response.status_code == 200
    assert get_response.json()['state']['questionnaire']['markdown'] == '# Updated Questionnaire\n\nnew body'


def test_update_questionnaire_markdown_endpoint_empty_markdown_422() -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        questionnaire=QuestionnaireDesign(
            title='Old Questionnaire',
            target_audience='users',
            objective='learn',
            introduction='intro',
            markdown='# Old Questionnaire',
        ),
        status='completed',
    )
    service.store.save_state(state)

    response = client.patch(f'/runs/{state.run_id}/questionnaire', json={'markdown': ''})

    assert response.status_code == 422


def test_update_questionnaire_markdown_endpoint_404_for_missing_run() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.patch('/runs/run_missing_questionnaire_update/questionnaire', json={'markdown': '# Updated'})

    assert response.status_code == 404


def test_update_questionnaire_markdown_endpoint_404_for_missing_questionnaire() -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()
    state = RunState(industry='saas', competitors=['alpha'], status='completed')
    service.store.save_state(state)

    response = client.patch(f'/runs/{state.run_id}/questionnaire', json={'markdown': '# Updated'})

    assert response.status_code == 404


def test_export_questionnaire_to_wenjuan_404_for_missing_run() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.post('/runs/run_missing_wjx_export/questionnaire/export/wenjuan')

    assert response.status_code == 404


def test_export_questionnaire_to_wenjuan_404_for_missing_questionnaire() -> None:
    app = create_app()
    client = TestClient(app)
    service = get_service()
    state = RunState(industry='saas', competitors=['alpha'], status='completed')
    service.store.save_state(state)

    response = client.post(f'/runs/{state.run_id}/questionnaire/export/wenjuan')

    assert response.status_code == 404


def test_export_questionnaire_to_wenjuan_503_when_disabled(monkeypatch) -> None:  # noqa: ANN001
    app = create_app()
    client = TestClient(app)
    service = get_service()
    monkeypatch.setattr(service.config, 'wjx_export_enabled', False)
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        questionnaire=QuestionnaireDesign(
            title='Questionnaire',
            target_audience='users',
            objective='learn',
            introduction='intro',
            markdown='# Questionnaire\n\n1. Question?\nA. Yes\nB. No',
        ),
        status='completed',
    )
    service.store.save_state(state)

    response = client.post(f'/runs/{state.run_id}/questionnaire/export/wenjuan')

    assert response.status_code == 503
    assert 'WJX_EXPORT_ENABLED' in response.json()['detail']


def test_export_questionnaire_to_wenjuan_success_persists_result(monkeypatch) -> None:  # noqa: ANN001
    app = create_app()
    client = TestClient(app)
    service = get_service()
    monkeypatch.setattr(service.config, 'wjx_export_enabled', True)
    monkeypatch.setattr(service.config, 'wjx_api_key', 'test-secret-key')
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        questionnaire=QuestionnaireDesign(
            title='Questionnaire',
            target_audience='users',
            objective='learn',
            introduction='intro',
            markdown='# Questionnaire\n\n1. Question?\nA. Yes\nB. No',
        ),
        status='completed',
    )
    service.store.save_state(state)

    def fake_export_questionnaire_with_wjx_cli(**kwargs):  # noqa: ANN003
        assert kwargs['run_id'] == state.run_id
        assert kwargs['api_key'] == 'test-secret-key'
        return {
            'provider': 'wjx',
            'status': 'success',
            'title': kwargs['title'],
            'url': 'https://www.wjx.cn/vm/test.aspx',
            'vid': 'test_vid',
            'exported_at': '2026-06-03T00:00:00+00:00',
            'jsonl_path': 'exports/questionnaire.jsonl',
            'raw_response': {'url': 'https://www.wjx.cn/vm/test.aspx'},
        }

    monkeypatch.setattr('app.core.workflow.export_questionnaire_with_wjx_cli', fake_export_questionnaire_with_wjx_cli)

    response = client.post(f'/runs/{state.run_id}/questionnaire/export/wenjuan')

    assert response.status_code == 200
    body = response.json()
    assert body['url'] == 'https://www.wjx.cn/vm/test.aspx'
    get_response = client.get(f'/runs/{state.run_id}')
    assert get_response.status_code == 200
    assert get_response.json()['state']['questionnaire_export']['vid'] == 'test_vid'
    workspace_response = client.get(f'/runs/{state.run_id}/workspace')
    assert workspace_response.status_code == 200
    assert workspace_response.json()['questionnaire_export']['url'] == 'https://www.wjx.cn/vm/test.aspx'


def test_export_questionnaire_to_wenjuan_cli_failure_redacts_error(monkeypatch) -> None:  # noqa: ANN001
    app = create_app()
    client = TestClient(app)
    service = get_service()
    monkeypatch.setattr(service.config, 'wjx_export_enabled', True)
    monkeypatch.setattr(service.config, 'wjx_api_key', 'test-secret-key')
    state = RunState(
        industry='saas',
        competitors=['alpha'],
        questionnaire=QuestionnaireDesign(
            title='Questionnaire',
            target_audience='users',
            objective='learn',
            introduction='intro',
            markdown='# Questionnaire\n\n1. Question?\nA. Yes\nB. No',
        ),
        status='completed',
    )
    service.store.save_state(state)

    def fake_export_questionnaire_with_wjx_cli(**kwargs):  # noqa: ANN003
        raise WjxCliError('问卷星 CLI 导出失败：API key *** rejected')

    monkeypatch.setattr('app.core.workflow.export_questionnaire_with_wjx_cli', fake_export_questionnaire_with_wjx_cli)

    response = client.post(f'/runs/{state.run_id}/questionnaire/export/wenjuan')

    assert response.status_code == 502
    assert 'test-secret-key' not in response.json()['detail']


def test_runs_replay_workspace_export_include_tool_events_agent_aggregation() -> None:
    app = create_app()
    client = TestClient(app)
    payload = {
        'industry': 'saas',
        'competitors': ['alpha'],
        'language': 'zh-CN',
        'timeframe': 'last_12_months',
    }
    create_resp = client.post('/runs', json=payload)
    assert create_resp.status_code == 200
    run_id = create_resp.json()['state']['run_id']

    service = get_service()
    service.store.append_event(
        EventRecord(
            run_id=run_id,
            stage=StageName.analyze,
            event_type='tool_event',
            payload={
                'event_type': 'tool.succeeded',
                'tool_name': 'web.search',
                'agent_name': 'AnalystAgent',
                'trace_name': 'agent.analyze.field.feature_tree',
                'tool_round': 1,
                'error_code': '',
            },
        )
    )
    service.store.append_event(
        EventRecord(
            run_id=run_id,
            stage=StageName.qa,
            event_type='tool_event',
            payload={
                'event_type': 'tool.failed',
                'tool_name': 'web.fetch',
                'agent_name': 'QACriticAgent',
                'trace_name': 'agent.qa.evaluate_report',
                'tool_round': 2,
                'error_code': 'http_429',
            },
        )
    )

    replay_resp = client.get(f'/runs/{run_id}/replay')
    assert replay_resp.status_code == 200
    replay_body = replay_resp.json()
    replay_tool_events = replay_body.get('tool_events', [])
    assert isinstance(replay_tool_events, list)
    replay_agents = {str(item.get('agent_name', '')).strip() for item in replay_tool_events if isinstance(item, dict)}
    assert {'AnalystAgent', 'QACriticAgent'}.issubset(replay_agents)

    workspace_resp = client.get(f'/runs/{run_id}/workspace')
    assert workspace_resp.status_code == 200
    workspace_body = workspace_resp.json()
    workspace_tool_events = workspace_body.get('observability', {}).get('tool_events', [])
    assert isinstance(workspace_tool_events, list)
    workspace_agents = {str(item.get('agent_name', '')).strip() for item in workspace_tool_events if isinstance(item, dict)}
    assert {'AnalystAgent', 'QACriticAgent'}.issubset(workspace_agents)

    export_resp = client.get(f'/runs/{run_id}/logs/export')
    assert export_resp.status_code == 200
    export_body = export_resp.json()
    export_tool_events = export_body.get('tool_events', [])
    assert isinstance(export_tool_events, list)
    export_agents = {str(item.get('agent_name', '')).strip() for item in export_tool_events if isinstance(item, dict)}
    assert {'AnalystAgent', 'QACriticAgent'}.issubset(export_agents)


def test_delete_run_endpoint() -> None:
    app = create_app()
    client = TestClient(app)
    payload = {
        'industry': 'saas',
        'competitors': ['alpha'],
        'language': 'zh-CN',
        'timeframe': 'last_12_months',
    }
    create_resp = client.post('/runs', json=payload)
    assert create_resp.status_code == 200
    run_id = create_resp.json()['state']['run_id']

    # ensure run exists first
    assert client.get(f'/runs/{run_id}').status_code == 200

    delete_resp = client.delete(f'/runs/{run_id}')
    assert delete_resp.status_code == 200
    assert delete_resp.json() == {'ok': True}

    # run and related endpoints should no longer find the data
    assert client.get(f'/runs/{run_id}').status_code == 404
    assert client.get(f'/runs/{run_id}/workspace').status_code == 404


def test_delete_run_endpoint_404() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.delete('/runs/run_not_exists_123')
    assert response.status_code == 404


def test_registry_endpoint() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get('/schema/registry')
    assert response.status_code == 200
    data = response.json()
    assert data['core'] == 'core_v1'
    assert 'saas' in data['domains']


def test_registry_by_industry() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get('/schema/registry?industry=saas')
    assert response.status_code == 200
    data = response.json()
    assert data['core'] == 'core_v1'
    assert data['active']['industry'] == 'saas'


def test_runtime_config_endpoint_masked() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get('/schema/runtime-config')
    assert response.status_code == 200
    body = response.json()
    assert 'openai_model' in body
    assert 'openai_base_url' in body
    assert 'openai_api_key_masked' in body
    assert 'openai_config_ready' in body


def test_proposals_endpoint() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get('/schema/proposals')
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_policies_and_field_risks_endpoints() -> None:
    app = create_app()
    client = TestClient(app)

    list_resp = client.get('/schema/policies?industry=saas')
    assert list_resp.status_code == 200
    assert isinstance(list_resp.json(), list)

    upsert_resp = client.post(
        '/schema/policies',
        json={
            'industry': 'saas',
            'enabled': True,
            'priority': 5,
            'max_fields': 5,
            'max_qa_failures': 2,
            'max_allowed_risk': 'medium',
            'denied_scopes': [],
            'decision': 'approved',
            'version': 'v1',
            'notes': 'test policy',
        },
    )
    assert upsert_resp.status_code == 200
    assert upsert_resp.json()['industry'] == 'saas'

    risks_upsert = client.post(
        '/schema/field-risks',
        json={
            'items': [
                {'industry': 'saas', 'field_name': 'new_metric', 'risk_level': 'low', 'notes': 'safe'},
            ]
        },
    )
    assert risks_upsert.status_code == 200
    assert len(risks_upsert.json()) == 1

    risks_list = client.get('/schema/field-risks?industry=saas')
    assert risks_list.status_code == 200
    assert isinstance(risks_list.json(), list)


def test_policy_audits_endpoint() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.get('/schema/policy-audits')
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_collector_health_endpoint() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.get('/collector/providers/health')
    assert resp.status_code == 200
    body = resp.json()
    assert 'search_order' in body
    assert 'fetch_order' in body
    assert isinstance(body['search_providers'], list)
    assert isinstance(body['fetch_providers'], list)


def test_collector_llm_health_endpoint() -> None:
    app = create_app()
    client = TestClient(app)
    cfg = get_config()
    old_key = cfg.openai_api_key
    old_base = cfg.openai_base_url
    old_model = cfg.openai_model
    cfg.openai_api_key = ''
    cfg.openai_base_url = ''
    cfg.openai_model = ''
    try:
        resp = client.get('/collector/llm/health')
        assert resp.status_code == 200
        body = resp.json()
        assert body['enabled'] is False
        assert body['success'] is False
        assert 'llm_call_status' in body
    finally:
        cfg.openai_api_key = old_key
        cfg.openai_base_url = old_base
        cfg.openai_model = old_model


def test_collector_preview_endpoint() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.post('/collector/preview', json={'prompt': '通用AI智能体竞品分析'})
    assert resp.status_code == 200
    body = resp.json()
    cfg = get_config()
    assert body['prompt'] == '通用AI智能体竞品分析'
    assert body['effective_max_urls'] == cfg.collector_max_urls
    assert 'direct' in body['candidate_groups']
    assert 'substitute' in body['candidate_groups']
    assert 'irrelevant' not in body['candidate_groups']
    assert isinstance(body['handoff_targets'], dict)
    assert isinstance(body['preview'], list)
    if body['preview']:
        item = body['preview'][0]
        assert 'search_events' in item
        assert 'fetch_events' in item
        assert 'fallback_trace' in item


def test_collector_preview_auto_save_enabled(tmp_path: Path) -> None:
    cfg = get_config()
    old_enabled = cfg.collector_preview_auto_save_enabled
    old_dir = cfg.collector_preview_save_dir
    cfg.collector_preview_auto_save_enabled = True
    cfg.collector_preview_save_dir = str(tmp_path)
    try:
        app = create_app()
        client = TestClient(app)
        resp = client.post('/collector/preview', json={'prompt': '通用AI智能体竞品分析'})
        assert resp.status_code == 200
        body = resp.json()
        assert body['auto_saved'] is True
        assert isinstance(body['auto_saved_file'], str) and body['auto_saved_file']
        assert re.search(r'collector_preview_result_\d{8}_\d{6}_[a-f0-9]{6}\.json$', body['auto_saved_file'])
        saved = Path(body['auto_saved_file'])
        assert saved.exists()
        data = json.loads(saved.read_text(encoding='utf-8'))
        assert data['prompt'] == '通用AI智能体竞品分析'
        assert 'preview' in data
        assert 'effective_max_urls' in data
    finally:
        cfg.collector_preview_auto_save_enabled = old_enabled
        cfg.collector_preview_save_dir = old_dir


def test_collector_preview_auto_save_disabled(tmp_path: Path) -> None:
    cfg = get_config()
    old_enabled = cfg.collector_preview_auto_save_enabled
    old_dir = cfg.collector_preview_save_dir
    cfg.collector_preview_auto_save_enabled = False
    cfg.collector_preview_save_dir = str(tmp_path)
    try:
        app = create_app()
        client = TestClient(app)
        resp = client.post('/collector/preview', json={'prompt': '通用AI智能体竞品分析'})
        assert resp.status_code == 200
        body = resp.json()
        assert body['auto_saved'] is False
        assert body['auto_saved_file'] == ''
        assert 'auto_saved_error' not in body
        assert not list(tmp_path.glob('collector_preview_result_*.json'))
    finally:
        cfg.collector_preview_auto_save_enabled = old_enabled
        cfg.collector_preview_save_dir = old_dir


def test_collector_preview_auto_save_error_keeps_success(tmp_path: Path) -> None:
    cfg = get_config()
    old_enabled = cfg.collector_preview_auto_save_enabled
    old_dir = cfg.collector_preview_save_dir
    blocked = tmp_path / 'blocked.txt'
    blocked.write_text('x', encoding='utf-8')
    cfg.collector_preview_auto_save_enabled = True
    cfg.collector_preview_save_dir = str(blocked)
    try:
        app = create_app()
        client = TestClient(app)
        resp = client.post('/collector/preview', json={'prompt': '通用AI智能体竞品分析'})
        assert resp.status_code == 200
        body = resp.json()
        assert body['auto_saved'] is False
        assert body['auto_saved_file'] == ''
        assert isinstance(body.get('auto_saved_error', ''), str) and body['auto_saved_error']
    finally:
        cfg.collector_preview_auto_save_enabled = old_enabled
        cfg.collector_preview_save_dir = old_dir
