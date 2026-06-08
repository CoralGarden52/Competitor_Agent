#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
import urllib.request
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent / 'backend'))

from app.core.config import get_config


def is_zhihu_url(url: str) -> bool:
    host = (urlparse(url).netloc or '').lower()
    return host.endswith('zhihu.com') or host.endswith('zhuanlan.zhihu.com')


def looks_like_risk_control(text: str) -> bool:
    lowered = text.lower()
    markers = [
        '安全验证',
        '验证码',
        '请完成安全验证',
        '请先完成验证',
        '访问受限',
        '登录知乎',
        'signin',
    ]
    return any(m.lower() in lowered for m in markers)


def firecrawl_fetch_markdown(api_key: str, url: str, timeout_sec: int) -> tuple[str, str]:
    if not api_key:
        return '', 'firecrawl_api_key_missing'
    try:
        req = urllib.request.Request(
            'https://api.firecrawl.dev/v1/scrape',
            data=json.dumps({'url': url, 'formats': ['markdown']}).encode('utf-8'),
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = resp.read().decode('utf-8', errors='ignore')
        data = json.loads(body)
        markdown = ''
        if isinstance(data, dict):
            markdown = (data.get('data') or {}).get('markdown', '') or ''
        if markdown:
            return markdown, ''
        return '', 'firecrawl_empty'
    except Exception as exc:
        return '', f'firecrawl_error: {exc}'


def diagnose_overall(rows: list[dict[str, object]]) -> str:
    total = len(rows)
    tavily_ok = sum(1 for r in rows if r.get('tavily_ok'))
    firecrawl_ok = sum(1 for r in rows if r.get('firecrawl_ok'))
    tavily_risk = sum(1 for r in rows if r.get('tavily_risk_suspected'))
    if total == 0:
        return '无样本，无法判断。'
    if tavily_ok == 0 and firecrawl_ok > 0:
        return '高概率是 Tavily->知乎 通道被策略限制（非你本机 IP）。'
    if tavily_ok == 0 and firecrawl_ok == 0:
        return '两条通道都失败，可能是知乎页面整体反爬严格或网络/配额问题。'
    if tavily_ok < firecrawl_ok:
        return 'Tavily 成功率明显低于 Firecrawl，存在通道层不稳定或被限迹象。'
    if tavily_risk > 0:
        return 'Tavily 有部分内容命中安全验证特征，疑似被反爬挑战。'
    return '两条通道表现接近，暂未见明显 Tavily 定向异常。'


def main() -> int:
    parser = argparse.ArgumentParser(description='Batch diagnose Zhihu crawl via tavily_extract vs firecrawl')
    parser.add_argument('--query', default='飞书 产品 site:zhihu.com', help='Search query for Tavily')
    parser.add_argument('--max-results', type=int, default=8, help='Max Tavily search results')
    parser.add_argument('--sample-size', type=int, default=5, help='Number of Zhihu URLs to test')
    args = parser.parse_args()

    config = get_config()
    if not config.tavily_api_key:
        print('ERROR: missing TAVILY_API_KEY in .env')
        return 2

    try:
        from tavily import TavilyClient
    except ImportError:
        print('ERROR: tavily-python not installed. Run: uv sync (or pip install tavily-python)')
        return 2

    client = TavilyClient(config.tavily_api_key)

    print('=' * 80)
    print('Step 1: Tavily search (find Zhihu URLs)')
    print('=' * 80)
    print(f'query={args.query}')

    try:
        search_resp = client.search(query=args.query, search_depth='advanced', max_results=args.max_results)
    except Exception as exc:
        print(f'ERROR: tavily search failed: {exc}')
        return 1

    results = search_resp.get('results', []) if isinstance(search_resp, dict) else []
    zhihu_hits = [r for r in results if is_zhihu_url(str(r.get('url', '')))]

    if not results:
        print('No search results returned.')
        return 1

    print(f'total_results={len(results)}; zhihu_results={len(zhihu_hits)}')
    for idx, r in enumerate(results, start=1):
        print(f"{idx}. {r.get('title', '')}\n   {r.get('url', '')}")

    if not zhihu_hits:
        print('No Zhihu URL found in search results, cannot continue extract test.')
        return 1

    print('\n' + '=' * 80)
    print('Step 2: Batch compare tavily_extract vs firecrawl')
    print('=' * 80)
    targets = zhihu_hits[: max(1, args.sample_size)]
    print(f'test_urls={len(targets)}')
    rows: list[dict[str, object]] = []
    for i, item in enumerate(targets, start=1):
        target_url = str(item.get('url', '')).strip()
        title = str(item.get('title', '')).strip()
        print(f'[{i}/{len(targets)}] {target_url}')

        tavily_error = ''
        tavily_content = ''
        tavily_resp: object = {}
        try:
            tavily_resp = client.extract(urls=[target_url])
            extract_results = tavily_resp.get('results', []) if isinstance(tavily_resp, dict) else []
            if extract_results and isinstance(extract_results[0], dict):
                tavily_content = str(extract_results[0].get('raw_content', '') or '')
            else:
                tavily_error = 'tavily_extract_empty'
        except Exception as exc:
            tavily_error = f'tavily_extract_error: {exc}'

        firecrawl_content, firecrawl_error = firecrawl_fetch_markdown(
            config.firecrawl_api_key,
            target_url,
            config.collector_provider_timeout_sec,
        )

        row = {
            'url': target_url,
            'title': title,
            'tavily_ok': bool(tavily_content),
            'tavily_len': len(tavily_content),
            'tavily_risk_suspected': looks_like_risk_control(tavily_content) if tavily_content else True,
            'tavily_error': tavily_error,
            'firecrawl_ok': bool(firecrawl_content),
            'firecrawl_len': len(firecrawl_content),
            'firecrawl_risk_suspected': looks_like_risk_control(firecrawl_content) if firecrawl_content else True,
            'firecrawl_error': firecrawl_error,
            'tavily_preview': tavily_content[:200].replace('\r', ' ').replace('\n', ' '),
            'firecrawl_preview': firecrawl_content[:200].replace('\r', ' ').replace('\n', ' '),
            'tavily_response': tavily_resp,
        }
        rows.append(row)
        print(
            f"  tavily_ok={row['tavily_ok']} len={row['tavily_len']} err={row['tavily_error']} | "
            f"firecrawl_ok={row['firecrawl_ok']} len={row['firecrawl_len']} err={row['firecrawl_error']}"
        )

    summary = {
        'sample_size': len(rows),
        'tavily_ok_count': sum(1 for r in rows if r['tavily_ok']),
        'firecrawl_ok_count': sum(1 for r in rows if r['firecrawl_ok']),
        'tavily_risk_suspected_count': sum(1 for r in rows if r['tavily_risk_suspected']),
        'firecrawl_risk_suspected_count': sum(1 for r in rows if r['firecrawl_risk_suspected']),
    }
    diagnosis = diagnose_overall(rows)
    print('\n' + '=' * 80)
    print('Diagnosis')
    print('=' * 80)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f'overall_diagnosis={diagnosis}')

    out = {
        'query': args.query,
        'total_results': len(results),
        'zhihu_results': len(zhihu_hits),
        'tested_urls': len(rows),
        'summary': summary,
        'overall_diagnosis': diagnosis,
        'details': rows,
    }
    output_path = Path('mock_data') / 'tavily_firecrawl_zhihu_batch_diagnosis.json'
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nSaved: {output_path}')
    return 0 if summary['tavily_ok_count'] > 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
