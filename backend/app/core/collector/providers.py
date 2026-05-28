from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from app.core.collector.types import FetchProvider, ProviderHealth, SearchHit, SearchProvider
from app.core.config import AppConfig

DEFAULT_JINA_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)


def _http_get_json(url: str, headers: dict[str, str], timeout: int) -> Any:
    req = urllib.request.Request(url, headers=headers, method='GET')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode('utf-8', errors='ignore')
    return json.loads(body)


def _http_post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> Any:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode('utf-8', errors='ignore')
    return json.loads(body)


def _http_post_text(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> str:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', errors='ignore')


class TavilySearchProvider:
    def __init__(self, config: AppConfig):
        self.config = config
        self.client = None
        if self.config.tavily_api_key:
            try:
                from tavily import TavilyClient
                self.client = TavilyClient(self.config.tavily_api_key)
            except ImportError:
                pass

    def name(self) -> str:
        return 'tavily'

    def health(self) -> ProviderHealth:
        ready = bool(self.client)
        return ProviderHealth(provider=self.name(), capabilities=['web_search'], available=ready, auth_ready=bool(self.config.tavily_api_key), note='api key required')

    def search(self, query: str, max_results: int) -> tuple[list[SearchHit], list[str]]:
        if not self.client:
            return [], ['tavily_unavailable: missing TAVILY_API_KEY or tavily-python not installed']
        start = time.time()
        try:
            response = self.client.search(query=query, search_depth="advanced", max_results=max_results)
            latency_ms = int((time.time() - start) * 1000)
            out = [
                SearchHit(query=query, title=r.get('title', ''), url=r.get('url', ''), snippet=r.get('content', ''), source_provider=self.name(), latency_ms=latency_ms)
                for r in response.get('results', [])
                if r.get('url')
            ]
            return out, []
        except Exception as exc:
            return [], [f'tavily_error: {exc}']


class QianfanSearchProvider:
    def __init__(self, config: AppConfig):
        self.config = config

    def name(self) -> str:
        return 'qianfan'

    def health(self) -> ProviderHealth:
        ready = bool(self.config.qianfan_api_key)
        return ProviderHealth(provider=self.name(), capabilities=['web_search'], available=ready, auth_ready=ready, note='bearer token required')

    def search(self, query: str, max_results: int) -> tuple[list[SearchHit], list[str]]:
        if not self.config.qianfan_api_key:
            return [], ['qianfan_unavailable: missing BAIDU_SEARCH_API_KEY']
        try:
            payload = {
                'messages': [{'role': 'user', 'content': query}],
                'search_source': 'baidu_search_v2',
                'edition': 'lite',
                'resource_type_filter': [{'type': 'web', 'top_k': max_results}],
            }
            data = _http_post_json(
                self.config.qianfan_search_endpoint,
                payload,
                {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.config.qianfan_api_key}',
                    'X-Appbuilder-Authorization': f'Bearer {self.config.qianfan_api_key}',
                },
                self.config.collector_provider_timeout_sec,
            )
            refs = data.get('references', []) if isinstance(data, dict) else []
            hits: list[SearchHit] = []
            for item in refs:
                if item.get('type') and item.get('type') != 'web':
                    continue
                url = item.get('url', '')
                if not url:
                    continue
                hits.append(
                    SearchHit(
                        query=query,
                        title=item.get('title') or item.get('web_anchor') or url,
                        url=url,
                        snippet=item.get('content', ''),
                        source_provider=self.name(),
                    )
                )
            return hits, []
        except Exception as exc:
            return [], [f'qianfan_unavailable: {exc}']


class ZhihuOfficialProvider:
    _RATE_LIMIT_RPS = 1.0
    _MIN_INTERVAL_SEC = 1.0 / _RATE_LIMIT_RPS
    _rate_limit_lock = threading.Lock()
    _last_request_monotonic = 0.0

    def __init__(self, config: AppConfig):
        self.config = config

    def name(self) -> str:
        return 'zhihu_official'

    def health(self) -> ProviderHealth:
        ready = bool(self.config.zhihu_search_access_secret or self.config.zhihu_client_secret)
        return ProviderHealth(provider=self.name(), capabilities=['web_search'], available=ready, auth_ready=ready, note='official access secret required')

    def search(self, query: str, max_results: int) -> tuple[list[SearchHit], list[str]]:
        secret = self.config.zhihu_search_access_secret or self.config.zhihu_client_secret
        if not secret:
            return [], ['zhihu_unavailable: missing ZHIHU_SEARCH_ACCESS_SECRET']
        try:
            self._wait_for_rate_limit_slot()
            timestamp = str(int(datetime.now(tz=timezone.utc).timestamp()))
            q = urllib.parse.quote(query, safe='')
            url = f'{self.config.zhihu_search_endpoint}?Query={q}&Count={min(max_results, 10)}'
            data = _http_get_json(
                url,
                headers={
                    'Authorization': f'Bearer {secret}',
                    'X-Request-Timestamp': timestamp,
                    'Content-Type': 'application/json',
                },
                timeout=self.config.collector_provider_timeout_sec,
            )
            items = data.get('Data', {}).get('Items', []) if isinstance(data, dict) else []
            hits = [
                SearchHit(
                    query=query,
                    title=item.get('Title', ''),
                    url=item.get('Url', ''),
                    snippet=item.get('ContentText', ''),
                    source_provider=self.name(),
                )
                for item in items
                if item.get('Url')
            ]
            return hits, []
        except Exception as exc:
            return [], [f'zhihu_unavailable: {exc}']

    @classmethod
    def _wait_for_rate_limit_slot(cls) -> None:
        """Apply a process-level rate limit for Zhihu search requests."""
        while True:
            with cls._rate_limit_lock:
                now = time.monotonic()
                elapsed = now - cls._last_request_monotonic
                if elapsed >= cls._MIN_INTERVAL_SEC:
                    cls._last_request_monotonic = now
                    return
                wait_sec = cls._MIN_INTERVAL_SEC - elapsed
            time.sleep(wait_sec)


class SerperSearchProvider:
    def __init__(self, config: AppConfig):
        self.config = config

    def name(self) -> str:
        return 'serper'

    def health(self) -> ProviderHealth:
        ready = bool(self.config.serper_api_key)
        return ProviderHealth(provider=self.name(), capabilities=['web_search'], available=ready, auth_ready=ready, note='api key required')

    def search(self, query: str, max_results: int) -> tuple[list[SearchHit], list[str]]:
        if not self.config.serper_api_key:
            return [], ['serper_unavailable: missing SERPER_API_KEY']
        try:
            data = _http_post_json(
                'https://google.serper.dev/search',
                {'q': query, 'num': max_results},
                {'X-API-KEY': self.config.serper_api_key, 'Content-Type': 'application/json'},
                self.config.collector_provider_timeout_sec,
            )
            org = data.get('organic', []) if isinstance(data, dict) else []
            hits = [
                SearchHit(query=query, title=item.get('title', ''), url=item.get('link', ''), snippet=item.get('snippet', ''), source_provider=self.name())
                for item in org
                if item.get('link')
            ]
            return hits, []
        except Exception as exc:
            return [], [f'serper_unavailable: {exc}']


class ExaSearchProvider:
    def __init__(self, config: AppConfig):
        self.config = config

    def name(self) -> str:
        return 'exa'

    def health(self) -> ProviderHealth:
        ready = bool(self.config.exa_api_key)
        return ProviderHealth(provider=self.name(), capabilities=['web_search'], available=ready, auth_ready=ready, note='api key required')

    def search(self, query: str, max_results: int) -> tuple[list[SearchHit], list[str]]:
        if not self.config.exa_api_key:
            return [], ['exa_unavailable: missing EXA_API_KEY']
        try:
            data = _http_post_json(
                'https://api.exa.ai/search',
                {'query': query, 'num_results': max_results},
                {'x-api-key': self.config.exa_api_key, 'Content-Type': 'application/json'},
                self.config.collector_provider_timeout_sec,
            )
            items = data.get('results', []) if isinstance(data, dict) else []
            hits = [
                SearchHit(query=query, title=item.get('title', ''), url=item.get('url', ''), snippet=item.get('text', ''), source_provider=self.name())
                for item in items
                if item.get('url')
            ]
            return hits, []
        except Exception as exc:
            return [], [f'exa_unavailable: {exc}']


class FirecrawlSearchProvider:
    def __init__(self, config: AppConfig):
        self.config = config

    def name(self) -> str:
        return 'firecrawl_search'

    def health(self) -> ProviderHealth:
        ready = bool(self.config.firecrawl_api_key)
        return ProviderHealth(provider=self.name(), capabilities=['web_search'], available=ready, auth_ready=ready, note='api key required')

    def search(self, query: str, max_results: int) -> tuple[list[SearchHit], list[str]]:
        if not self.config.firecrawl_api_key:
            return [], ['firecrawl_search_unavailable: missing FIRECRAWL_API_KEY']
        try:
            data = _http_post_json(
                'https://api.firecrawl.dev/v1/search',
                {'query': query, 'limit': max_results},
                {'Authorization': f'Bearer {self.config.firecrawl_api_key}', 'Content-Type': 'application/json'},
                self.config.collector_provider_timeout_sec,
            )
            items = data.get('data', []) if isinstance(data, dict) else []
            hits = [
                SearchHit(query=query, title=item.get('title', ''), url=item.get('url', ''), snippet=item.get('description', ''), source_provider=self.name())
                for item in items
                if item.get('url')
            ]
            return hits, []
        except Exception as exc:
            return [], [f'firecrawl_search_unavailable: {exc}']


class JinaFetchProvider:
    def __init__(self, config: AppConfig):
        self.config = config

    def name(self) -> str:
        return 'jina'

    def health(self) -> ProviderHealth:
        auth_ready = bool(self.config.jina_api_key.strip())
        note = 'jina reader api (api key configured)' if auth_ready else 'jina reader api (missing JINA_API_KEY; may hit stricter limits)'
        return ProviderHealth(provider=self.name(), capabilities=['web_fetch'], available=True, auth_ready=auth_ready, note=note)

    def fetch(self, url: str) -> tuple[str, list[str]]:
        try:
            timeout = self.config.collector_provider_timeout_sec
            user_agent = self.config.jina_user_agent.strip() or DEFAULT_JINA_USER_AGENT
            headers = {
                'Content-Type': 'application/json',
                'X-Return-Format': 'markdown',
                'X-Timeout': str(timeout),
                'User-Agent': user_agent,
            }
            jina_api_key = self.config.jina_api_key.strip() or os.getenv('JINA_API_KEY', '').strip()
            if jina_api_key:
                headers['Authorization'] = f'Bearer {jina_api_key}'
            content = _http_post_text('https://r.jina.ai/', {'url': url}, headers, timeout)
            if content.strip():
                return content, []
            return '', ['jina_fetch_failed: jina api returned empty response']
        except urllib.error.HTTPError as exc:
            host = urllib.parse.urlparse(url).netloc or 'unknown-host'
            if exc.code == 401:
                return '', [f'jina_fetch_failed: HTTP 401 Unauthorized (check JINA_API_KEY) host={host}']
            if exc.code == 403:
                return '', [f'jina_fetch_failed: HTTP 403 Forbidden (target blocked or key lacks access) host={host}']
            if exc.code == 429:
                return '', [f'jina_fetch_failed: HTTP 429 Too Many Requests (rate limited) host={host}']
            return '', [f'jina_fetch_failed: HTTP {exc.code} {exc.reason} host={host}']
        except Exception as exc:
            host = urllib.parse.urlparse(url).netloc or 'unknown-host'
            return '', [f'jina_fetch_failed: {exc} host={host}']


class FirecrawlFetchProvider:
    def __init__(self, config: AppConfig):
        self.config = config

    def name(self) -> str:
        return 'firecrawl_fetch'

    def health(self) -> ProviderHealth:
        ready = bool(self.config.firecrawl_api_key)
        return ProviderHealth(provider=self.name(), capabilities=['web_fetch'], available=ready, auth_ready=ready, note='api key required')

    def fetch(self, url: str) -> tuple[str, list[str]]:
        if not self.config.firecrawl_api_key:
            return '', ['firecrawl_fetch_unavailable: missing FIRECRAWL_API_KEY']
        try:
            data = _http_post_json(
                'https://api.firecrawl.dev/v1/scrape',
                {'url': url, 'formats': ['markdown']},
                {'Authorization': f'Bearer {self.config.firecrawl_api_key}', 'Content-Type': 'application/json'},
                self.config.collector_provider_timeout_sec,
            )
            content = ''
            if isinstance(data, dict):
                content = (data.get('data') or {}).get('markdown', '')
            return content or '', ([] if content else ['firecrawl_fetch_empty'])
        except Exception as exc:
            return '', [f'firecrawl_fetch_unavailable: {exc}']


class TavilyExtractProvider:
    def __init__(self, config: AppConfig):
        self.config = config
        self._client = None

    def name(self) -> str:
        return 'tavily_extract'

    def health(self) -> ProviderHealth:
        ready = bool(self.config.tavily_api_key)
        return ProviderHealth(provider=self.name(), capabilities=['web_fetch'], available=ready, auth_ready=ready, note='api key required')

    def fetch(self, url: str) -> tuple[str, list[str]]:
        if not self.config.tavily_api_key:
            return '', ['tavily_extract_unavailable: missing TAVILY_API_KEY']
        try:
            from tavily import TavilyClient
            
            if self._client is None:
                self._client = TavilyClient(self.config.tavily_api_key)
            
            response = self._client.extract(urls=[url])
            results = response.get('results', []) if isinstance(response, dict) else []
            if not results:
                return '', ['tavily_extract_empty']
            content = results[0].get('raw_content', '') or ''
            return content, ([] if content else ['tavily_extract_empty'])
        except ImportError:
            return '', ['tavily_extract_unavailable: tavily package not installed']
        except Exception as exc:
            return '', [f'tavily_extract_unavailable: {exc}']


def build_search_provider_catalog(config: AppConfig) -> dict[str, SearchProvider]:
    return {
        'qianfan': QianfanSearchProvider(config),
        'tavily': TavilySearchProvider(config),
        'serper': SerperSearchProvider(config),
        'exa': ExaSearchProvider(config),
        'firecrawl_search': FirecrawlSearchProvider(config),
        'zhihu_official': ZhihuOfficialProvider(config),
    }


def build_fetch_provider_catalog(config: AppConfig) -> dict[str, FetchProvider]:
    return {
        'jina': JinaFetchProvider(config),
        'firecrawl_fetch': FirecrawlFetchProvider(config),
        'tavily_extract': TavilyExtractProvider(config),
    }
