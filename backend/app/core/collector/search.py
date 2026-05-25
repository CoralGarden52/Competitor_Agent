from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.collector.provider_registry import ProviderRegistry
    from app.core.collector.types import SearchHit


def search_with_fallback(
    query: str,
    registry: ProviderRegistry,
    fallback_trace: list[dict],
    max_results: int = 8,
) -> tuple[list[SearchHit], list[dict]]:
    """
    使用 fallback 机制执行搜索。
    依次尝试每个搜索 provider，直到成功获取结果。
    """
    trace: list[dict] = []
    hits: list[SearchHit] = []
    
    for provider in registry.ordered_search_providers():
        try:
            provider_hits, provider_errors = provider.search(query, max_results)
            
            if provider_hits:
                hits.extend(provider_hits)
                trace.append({
                    'provider': provider.name(),
                    'status': 'success',
                    'hit_count': len(provider_hits),
                })
                break
            else:
                trace.append({
                    'provider': provider.name(),
                    'status': 'empty',
                    'errors': provider_errors,
                })
        except Exception as e:
            trace.append({
                'provider': provider.name(),
                'status': 'error',
                'error': str(e),
            })
    
    fallback_trace.extend(trace)
    return hits, trace
