from __future__ import annotations

from dataclasses import dataclass

from app.core.storage import SQLiteStore

CORE_SCHEMA_VERSION = 'core_v1'


@dataclass(frozen=True)
class DomainSchema:
    industry: str
    version: str
    required_extension_fields: tuple[str, ...]


def get_domain_schema(store: SQLiteStore, industry: str) -> DomainSchema:
    row = store.get_active_domain_schema(industry)
    return DomainSchema(
        industry=row['industry'],
        version=row['version'],
        required_extension_fields=tuple(row['required_extension_fields']),
    )


def registry_snapshot(store: SQLiteStore, industry: str | None = None) -> dict[str, object]:
    if industry:
        current = store.get_active_domain_schema(industry)
        return {
            'core': CORE_SCHEMA_VERSION,
            'industry': industry,
            'active': current,
        }

    domains = store.list_active_domain_schemas()
    return {
        'core': CORE_SCHEMA_VERSION,
        'domains': {
            item['industry']: {'version': item['version'], 'required_extension_fields': item['required_extension_fields']} for item in domains
        },
    }
