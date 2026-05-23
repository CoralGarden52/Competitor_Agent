from __future__ import annotations


def dedup_by_url_and_hash(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for item in items:
        key = (item.get('source_url', ''), item.get('content_hash', ''))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def verify_cross_source(items: list[dict]) -> list[dict]:
    provider_counts: dict[str, int] = {}
    for item in items:
        provider = item.get('source_provider', 'unknown')
        provider_counts[provider] = provider_counts.get(provider, 0) + 1

    for item in items:
        item['cross_source_ok'] = len(provider_counts) >= 2
        if not item['cross_source_ok']:
            item['risk_flag'] = True
    return items
