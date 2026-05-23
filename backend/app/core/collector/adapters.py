from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class CollectResult:
    search_query: str
    items: list[dict[str, Any]]
    provider: str
    errors: list[str]


class DeerFlowCollectorAdapter:
    """Reuse deer-flow community tools when available."""

    def __init__(self) -> None:
        self._ensure_deerflow_path()

    def _ensure_deerflow_path(self) -> None:
        repo_root = Path(__file__).resolve().parents[5]
        harness_path = repo_root / 'backend' / 'packages' / 'harness'
        if harness_path.exists() and str(harness_path) not in sys.path:
            sys.path.append(str(harness_path))

    def collect_competitor(self, competitor: str, industry: str, max_results: int = 3) -> CollectResult:
        query = f'{competitor} {industry} pricing features user reviews'
        errors: list[str] = []
        items: list[dict[str, Any]] = []

        search_results = self._search(query, max_results=max_results, errors=errors)
        for result in search_results[:max_results]:
            url = result.get('url') or ''
            if not url:
                continue
            content = self._fetch(url, errors=errors)
            items.append(
                {
                    'title': result.get('title', ''),
                    'url': url,
                    'snippet': result.get('content', ''),
                    'content': content,
                    'source_type': self._infer_source_type(url),
                }
            )

        return CollectResult(search_query=query, items=items, provider='deerflow.community', errors=errors)

    def _search(self, query: str, max_results: int, errors: list[str]) -> list[dict[str, Any]]:
        try:
            from deerflow.community.ddg_search.tools import web_search_tool

            output = web_search_tool.invoke({'query': query, 'max_results': max_results})
            payload = json.loads(output) if isinstance(output, str) else output
            return payload.get('results', []) if isinstance(payload, dict) else []
        except Exception as exc:
            errors.append(f'web_search_failed: {exc}')
            return []

    def _fetch(self, url: str, errors: list[str]) -> str:
        try:
            from deerflow.community.jina_ai.tools import web_fetch_tool

            if hasattr(web_fetch_tool, 'coroutine') and web_fetch_tool.coroutine is not None:
                return str(asyncio.run(web_fetch_tool.coroutine(url=url)))
            return str(web_fetch_tool.invoke({'url': url}))
        except Exception as exc:
            errors.append(f'web_fetch_failed({url}): {exc}')
            return ''

    @staticmethod
    def _infer_source_type(url: str) -> str:
        domain = url.lower()
        if 'docs.' in domain or 'help.' in domain:
            return 'official'
        if 'news' in domain or 'techcrunch' in domain or 'forbes' in domain:
            return 'news'
        if 'reddit' in domain or 'github' in domain:
            return 'community'
        if 'g2.com' in domain or 'capterra' in domain:
            return 'review'
        return 'report'
