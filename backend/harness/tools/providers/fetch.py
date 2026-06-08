from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.tools.providers.registry import ProviderRegistry


def fetch_with_fallback(
    url: str,
    registry: ProviderRegistry,
    fallback_trace: list[dict],
) -> tuple[str, str, list[dict]]:
    """
    使用 fallback 机制执行抓取。
    依次尝试每个抓取 provider，直到成功获取内容。
    """
    trace: list[dict] = []
    content = ''
    provider_name = ''
    
    for provider in registry.ordered_fetch_providers():
        try:
            provider_content, provider_errors = provider.fetch(url)
            
            if provider_content and len(provider_content) > 100:
                content = provider_content
                provider_name = provider.name()
                trace.append({
                    'provider': provider_name,
                    'status': 'success',
                    'content_length': len(content),
                })
                break
            elif provider_content:
                # 内容太短，继续尝试下一个 provider
                content = provider_content
                provider_name = provider.name()
                trace.append({
                    'provider': provider_name,
                    'status': 'partial',
                    'content_length': len(content),
                    'errors': provider_errors,
                })
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
    return content, provider_name, trace
