from __future__ import annotations

import json
import re
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
    assert body['state']['status'] in ('completed', 'failed')
    assert len(body['state']['profiles']) >= 1

    profile = body['state']['profiles'][0]
    assert 'feature_tree' in profile
    assert 'advantages' in profile
    assert 'disadvantages' in profile
    assert 'pricing_model' in profile
    assert 'user_feedback' in profile


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
