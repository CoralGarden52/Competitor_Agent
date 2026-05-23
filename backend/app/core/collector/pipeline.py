from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.collector.extractor import extract_fields, mask_pii
from app.core.collector.normalizer import content_hash, normalize_url, recency_score
from app.core.collector.provider_registry import ProviderRegistry
from app.core.collector.providers import build_fetch_provider_catalog, build_search_provider_catalog
from app.core.collector.query_planner import build_queries
from app.core.collector.types import CollectorOutput
from app.core.collector.verifier import dedup_by_url_and_hash, verify_cross_source
from app.core.config import AppConfig
from app.core.storage import SQLiteStore


class CollectorPipeline:
    def __init__(self, config: AppConfig, store: SQLiteStore):
        self.config = config
        self.store = store
        self.registry = ProviderRegistry(
            search_catalog=build_search_provider_catalog(config),
            fetch_catalog=build_fetch_provider_catalog(config),
            search_order=config.collector_search_order_list,
            fetch_order=config.collector_fetch_order_list,
        )

    def provider_health(self) -> dict:
        search = [p.health().__dict__ for p in self.registry.ordered_search_providers()]
        fetch = [p.health().__dict__ for p in self.registry.ordered_fetch_providers()]
        return {
            'search_order': self.registry.search_provider_names(),
            'fetch_order': self.registry.fetch_provider_names(),
            'search_providers': search,
            'fetch_providers': fetch,
        }

    def collect(
        self,
        *,
        run_id: str,
        industry: str,
        competitor: str,
        max_urls: int | None = None,
        schema_plan: list[dict] | None = None,
        per_field_limit: int = 3,
    ) -> CollectorOutput:
        output = CollectorOutput()
        fields = self._resolve_schema_fields(competitor=competitor, industry=industry, schema_plan=schema_plan)
        candidate_rows: list[dict] = []
        fallback_trace: list[dict] = []
        max_items = max_urls if max_urls is not None and max_urls > 0 else None

        for field_plan in fields:
            field_name = field_plan['field_name']
            queries = field_plan['queries']
            recommended_sources = field_plan['recommended_sources']
            output.provider_events.append(
                {
                    'event_type': 'collector.field_query.started',
                    'competitor': competitor,
                    'field_name': field_name,
                    'query_count': len(queries),
                }
            )
            field_hits = 0
            for query in queries:
                if max_items is not None and len(candidate_rows) >= max_items:
                    break
                if field_hits >= per_field_limit:
                    output.provider_events.append(
                        {
                            'event_type': 'collector.field_quota_reached',
                            'competitor': competitor,
                            'field_name': field_name,
                            'limit': per_field_limit,
                        }
                    )
                    break
                search_hits = self._run_search_phase(query=query, output=output, fallback_trace=fallback_trace)
                for hit in search_hits:
                    if max_items is not None and len(candidate_rows) >= max_items:
                        break
                    if field_hits >= per_field_limit:
                        output.provider_events.append(
                            {
                                'event_type': 'collector.field_quota_reached',
                                'competitor': competitor,
                                'field_name': field_name,
                                'limit': per_field_limit,
                            }
                        )
                        break
                    source_url = normalize_url(hit.url)
                    content, fetch_provider = self._run_fetch_phase(url=source_url, output=output, fallback_trace=fallback_trace)
                    sanitized = mask_pii(content or hit.snippet)
                    extracted_fields = extract_fields(sanitized, hit.snippet)
                    h = content_hash(sanitized or hit.snippet)
                    local_path = self._persist_raw_content(run_id=run_id, evidence_hash=h, content=sanitized or hit.snippet)
                    captured = datetime.now(UTC)
                    retrieval_status = 'ok' if content else 'partial'
                    candidate_rows.append(
                        {
                            'query': hit.query,
                            'title': hit.title,
                            'source_url': source_url,
                            'snippet': hit.snippet[:500],
                            'source_provider': hit.source_provider,
                            'source_type': self._infer_source_type(source_url),
                            'retrieval_method': f'{hit.source_provider}+{fetch_provider}',
                            'retrieval_status': retrieval_status,
                            'captured_at': captured,
                            'extract_fields': extracted_fields,
                            'confidence': 0.72 if content else 0.55,
                            'recency_score': recency_score(captured),
                            'license_or_tos_note': 'public web source, compliance checkpoint recorded',
                            'raw_content_path': str(local_path),
                            'content_hash': h,
                            'content_excerpt': (sanitized or hit.snippet)[:1000],
                            'latency_ms': hit.latency_ms,
                            'error_code': '' if content else 'fetch_fallback_partial',
                            'schema_field': field_name,
                            'query_template': query,
                            'recommended_source_type': ','.join(recommended_sources),
                        }
                    )
                    field_hits += 1
            output.provider_events.append(
                {
                    'event_type': 'collector.field_query.completed',
                    'competitor': competitor,
                    'field_name': field_name,
                    'evidence_count': field_hits,
                }
            )

        candidate_rows = dedup_by_url_and_hash(candidate_rows)
        candidate_rows = verify_cross_source(candidate_rows)
        if max_items is not None:
            candidate_rows = candidate_rows[:max_items]
        output.evidences = candidate_rows
        output.provider_events.append({'event_type': 'collector.fallback.trace', 'fallback_trace': fallback_trace})
        return output

    def _resolve_schema_fields(self, *, competitor: str, industry: str, schema_plan: list[dict] | None) -> list[dict]:
        if not schema_plan:
            return [
                {'field_name': '默认', 'queries': build_queries(competitor, industry), 'recommended_sources': ['public_web']},
            ]
        output: list[dict] = []
        for item in schema_plan:
            if not isinstance(item, dict):
                continue
            field_name = self._repair_mojibake(str(item.get('field_name', '')).strip())
            if not field_name:
                continue
            templates = item.get('query_templates', [])
            if not isinstance(templates, list):
                templates = []
            queries = [self._repair_mojibake(str(t)).replace('{product}', competitor).strip() for t in templates if str(t).strip()]
            if not queries:
                queries = [f'{competitor} {industry} {field_name}']
            sources = item.get('recommended_sources', [])
            if not isinstance(sources, list):
                sources = []
            output.append(
                {
                    'field_name': field_name,
                    'queries': queries,
                    'recommended_sources': [self._repair_mojibake(str(s)) for s in sources],
                }
            )
        return output or [{'field_name': '默认', 'queries': build_queries(competitor, industry), 'recommended_sources': ['public_web']}]

    @staticmethod
    def _repair_mojibake(text: str) -> str:
        if not text:
            return text
        markers = ('Ã', 'Â', 'ä', 'å', 'ç', 'æ', 'é', 'è', 'ê', 'ï', 'ð')
        if any(m in text for m in markers):
            try:
                repaired = text.encode('latin-1').decode('utf-8')
                if repaired:
                    return repaired
            except Exception:
                return text
        return text

    def _run_search_phase(self, *, query: str, output: CollectorOutput, fallback_trace: list[dict]) -> list:
        output.provider_events.append({'event_type': 'collector.search.started', 'query': query})
        providers = self.registry.ordered_search_providers()
        if '知乎' in query.lower() or 'zhihu' in query.lower() or 'user_feedback' in query.lower():
            providers = sorted(providers, key=lambda p: 0 if p.name() == 'zhihu_official' else 1)
        for index, provider in enumerate(providers):
            hits: list = []
            errors: list[str] = []
            for _ in range(self.config.collector_provider_retry + 1):
                hits, errors = provider.search(query, self.config.collector_max_results_per_query)
                if hits:
                    break
            if hits:
                output.provider_events.append(
                    {
                        'event_type': 'collector.search.completed',
                        'provider': provider.name(),
                        'query': query,
                        'hit_count': len(hits),
                    }
                )
                return hits
            output.provider_events.append(
                {
                    'event_type': 'collector.search.failed',
                    'provider': provider.name(),
                    'query': query,
                    'errors': errors,
                }
            )
            output.errors.extend(errors)
            if index < len(providers) - 1:
                next_provider = providers[index + 1].name()
                fallback = {'event_type': 'collector.provider.fallback', 'phase': 'search', 'from': provider.name(), 'to': next_provider, 'query': query}
                fallback_trace.append(fallback)
                output.provider_events.append(fallback)
        output.provider_events.append({'event_type': 'collector.search.failed', 'query': query, 'errors': ['all_search_providers_failed']})
        return []

    def _run_fetch_phase(self, *, url: str, output: CollectorOutput, fallback_trace: list[dict]) -> tuple[str, str]:
        output.provider_events.append({'event_type': 'collector.fetch.started', 'url': url})
        cache_status = self._get_cache_status(url)
        if cache_status is not None:
            status, cached = cache_status
            if status == 'hit':
                output.provider_events.append({'event_type': 'collector.fetch.cache_hit', 'url': url})
                return str(cached['content']), f"cache+{cached['source_provider']}"
            if status == 'refresh':
                output.provider_events.append({'event_type': 'collector.fetch.cache_refresh', 'url': url})
        else:
            output.provider_events.append({'event_type': 'collector.fetch.cache_miss', 'url': url})

        ordered = self.registry.ordered_fetch_providers()
        for index, provider in enumerate(ordered):
            content = ''
            errors: list[str] = []
            for _ in range(self.config.collector_provider_retry + 1):
                content, errors = provider.fetch(url)
                if content:
                    break
            if content:
                self.store.upsert_cached_page(
                    url=url,
                    content=content,
                    content_hash=content_hash(content),
                    source_provider=provider.name(),
                )
                output.provider_events.append({'event_type': 'collector.fetch.completed', 'provider': provider.name(), 'url': url, 'content_len': len(content)})
                return content, provider.name()
            output.provider_events.append({'event_type': 'collector.fetch.failed', 'provider': provider.name(), 'url': url, 'errors': errors})
            output.errors.extend(errors)
            if index < len(ordered) - 1:
                next_provider = ordered[index + 1].name()
                fallback = {'event_type': 'collector.provider.fallback', 'phase': 'fetch', 'from': provider.name(), 'to': next_provider, 'url': url}
                fallback_trace.append(fallback)
                output.provider_events.append(fallback)
        return '', 'none'

    def _get_cache_status(self, url: str) -> tuple[str, dict] | None:
        if not self.config.collector_cache_enabled:
            return None
        cached = self.store.get_cached_page(url)
        if cached is None:
            return None
        try:
            last_checked = datetime.fromisoformat(str(cached['last_checked_at']))
        except Exception:
            return ('refresh', cached)
        if datetime.now(UTC) - last_checked <= timedelta(days=self.config.collector_cache_ttl_days):
            return ('hit', cached)
        return ('refresh', cached)

    def _persist_raw_content(self, *, run_id: str, evidence_hash: str, content: str) -> Path:
        root = Path(self.config.sqlite_path).parent / 'raw_evidence' / run_id
        root.mkdir(parents=True, exist_ok=True)
        target = root / f'{evidence_hash}.txt'
        if not target.exists():
            target.write_text(content, encoding='utf-8', errors='ignore')
        return target

    @staticmethod
    def _infer_source_type(url: str) -> str:
        u = url.lower()
        if 'zhihu.com' in u or 'reddit.com' in u:
            return 'community'
        if 'news' in u or 'forbes' in u or 'techcrunch' in u:
            return 'news'
        if 'g2.com' in u or 'capterra' in u:
            return 'review'
        if 'docs.' in u or 'help.' in u or 'developer.' in u:
            return 'official'
        return 'report'
