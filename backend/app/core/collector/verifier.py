from __future__ import annotations

from urllib.parse import urlparse


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
    hosts_by_field: dict[str, set[str]] = {}
    for item in items:
        field_name = str(item.get('schema_field', '') or '')
        host = urlparse(str(item.get('source_url', '') or '')).netloc.casefold()
        if host:
            hosts_by_field.setdefault(field_name, set()).add(host)
    for item in items:
        source_host_count = len(hosts_by_field.get(str(item.get('schema_field', '') or ''), set()))
        item['source_host_count'] = source_host_count
        item['cross_source_ok'] = source_host_count >= 2
        if not item['cross_source_ok']:
            item['risk_flag'] = True
    return items
