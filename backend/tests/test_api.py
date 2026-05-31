from __future__ import annotations

import json
import re
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_config
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
    for _ in range(40):
        final_body = client.get(f'/runs/{run_id}').json()
        if final_body['state']['status'] in ('completed', 'failed'):
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
    assert any(item['handoff_type'] == 'PlanHandoff' for item in body['handoffs'])

    node_resp = client.get(f'/runs/{run_id}/nodes/analyze')
    assert node_resp.status_code == 200
    node_body = node_resp.json()
    assert isinstance(node_body['handoffs'], list)


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
