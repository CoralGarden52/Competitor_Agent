from __future__ import annotations

import concurrent.futures
import re
from datetime import UTC, datetime
from pathlib import Path
import time
from urllib.parse import urlparse
from typing import Any

from app.core.collector.normalizer import content_hash, normalize_url, recency_score
from app.core.collector.query_planner import build_queries
from app.core.collector.types import CollectorOutput
from app.core.collector.verifier import dedup_by_url_and_hash, verify_cross_source
from app.core.config import AppConfig
from app.core.models import AnalysisSchemaField
from app.core.storage import PostgresStore
from harness.tools import ToolRequest, ToolRouter
from harness.tools.bootstrap import build_tool_runtime
from harness.tools.providers import ProviderRegistry, SearchHit


class CollectorPipeline:
    def __init__(self, config: AppConfig, store: PostgresStore, tool_router: ToolRouter | None = None, provider_registry: ProviderRegistry | None = None):
        self.config = config
        self.store = store
        runtime = build_tool_runtime(config) if tool_router is None else None
        self.tool_router = tool_router or runtime.router
        self.registry = provider_registry or (runtime.provider_registry if runtime is not None else None)

    def provider_health(self) -> dict:
        if self.registry is None:
            return {}
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
        schema_plan: list[AnalysisSchemaField] | list[dict] | None = None,
        per_field_limit: int = 3,
        field_query_overrides: dict[str, list[str]] | None = None,
        target_fields: set[str] | None = None,
    ) -> CollectorOutput:
        output = CollectorOutput()
        fields = self._resolve_schema_fields(
            competitor=competitor,
            industry=industry,
            schema_plan=schema_plan,
            field_query_overrides=field_query_overrides,
        )
        if target_fields:
            fields = [item for item in fields if str(item.get('field_name', '')).strip() in target_fields]
        candidate_rows: list[dict] = []
        fallback_trace: list[dict] = []
        max_items = max_urls if max_urls is not None and max_urls > 0 else None

        fetch_tasks: list[tuple[str, str, str, list[str], str, str, str]] = []
        search_tasks: list[tuple[str, str, list[str], int, list[str] | None, str]] = []

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
            for query in queries:
                if field_name == 'pricing_model':
                    provider_allowlist = ['tavily']
                    search_strategy = 'strict_tavily_only'
                else:
                    provider_allowlist = None
                    search_strategy = 'default_fallback'
                search_tasks.append((field_name, query, recommended_sources, per_field_limit, provider_allowlist, search_strategy))

        def _search_one(
            task: tuple[str, str, list[str], int, list[str] | None, str],
            *,
            startup_delay_sec: float = 0.0,
        ) -> list[tuple[str, str, str, list[str], str, str, str]]:
            if startup_delay_sec > 0:
                time.sleep(startup_delay_sec)
            field_name, query, recommended_sources, _, provider_allowlist, search_strategy = task
            local_fallback_trace: list[dict] = []
            search_hits = self._run_search_phase(
                query=query,
                output=output,
                fallback_trace=local_fallback_trace,
                provider_allowlist=provider_allowlist,
                field_name=field_name,
                strategy=search_strategy,
            )
            fallback_trace.extend(local_fallback_trace)
            results: list[tuple[str, str, str, list[str], str, str, str]] = []
            for hit in search_hits:
                source_url = normalize_url(hit.url)
                results.append((field_name, hit.query, source_url, recommended_sources, hit.title, hit.snippet, hit.source_provider))
            return results

        all_search_results: list[tuple[str, str, str, list[str], str, str, str]] = []
        if search_tasks:
            search_max_workers = min(len(search_tasks), 6)
            with concurrent.futures.ThreadPoolExecutor(max_workers=search_max_workers) as executor:
                futures = [
                    executor.submit(
                        _search_one,
                        task,
                        startup_delay_sec=(index % search_max_workers) * 0.5,
                    )
                    for index, task in enumerate(search_tasks)
                ]
                for future in concurrent.futures.as_completed(futures):
                    all_search_results.extend(future.result())
        all_search_results = self._prioritize_search_results(all_search_results, output, competitor)

        field_hit_counts: dict[str, int] = {}
        for field_name, query, source_url, recommended_sources, title, snippet, source_provider in all_search_results:
            if max_items is not None and len(fetch_tasks) >= max_items:
                break

            current_hits = field_hit_counts.get(field_name, 0)
            field_fetch_limit = self._field_prefetch_limit(field_name=field_name, per_field_limit=per_field_limit)
            if current_hits >= field_fetch_limit:
                output.provider_events.append(
                    {
                        'event_type': 'collector.field_quota_reached',
                        'competitor': competitor,
                        'field_name': field_name,
                        'limit': field_fetch_limit,
                    }
                )
                continue
            fetch_tasks.append((field_name, query, source_url, recommended_sources, title, snippet, source_provider))
            field_hit_counts[field_name] = current_hits + 1

        for field_name in field_hit_counts:
            output.provider_events.append(
                {
                    'event_type': 'collector.field_query.completed',
                    'competitor': competitor,
                    'field_name': field_name,
                    'evidence_count': field_hit_counts[field_name],
                }
            )

        def _fetch_one(task: tuple[str, str, str, list[str], str, str, str]) -> dict | None:
            field_name, query, source_url, recommended_sources, search_title, search_snippet, source_provider = task
            local_fallback_trace: list[dict] = []

            provider_order = self.config.collector_fetch_order_list or ['jina', 'firecrawl_fetch', 'tavily_extract']
            fetch_strategy = 'unified_schema_fetch'
            content, fetch_provider = self._run_fetch_phase(
                url=source_url,
                output=output,
                fallback_trace=local_fallback_trace,
                provider_order=provider_order,
                field_name=field_name,
                strategy=fetch_strategy,
            )
            fallback_trace.extend(local_fallback_trace)

            if content and len(content) > 100:
                extracted = self.tool_router.invoke(
                    ToolRequest(
                        name='web.extract',
                        args={'content': content, 'title': search_title or competitor, 'snippet': search_snippet},
                        metadata={'group': 'web'},
                    )
                )
                output.tool_events.append(
                    {'event_type': 'collector.tool.extract', 'tool_name': 'web.extract', 'ok': extracted.ok}
                )
                sanitized = str(extracted.output.get('sanitized', ''))
                extracted_fields = extracted.output.get('extract_fields', {})
                retrieval_status = 'ok'
                snippet = sanitized[:500]
                confidence = 0.72
            else:
                extracted = self.tool_router.invoke(
                    ToolRequest(
                        name='web.extract',
                        args={'content': search_snippet or content, 'title': search_title or competitor, 'snippet': search_snippet},
                        metadata={'group': 'web'},
                    )
                )
                output.tool_events.append(
                    {'event_type': 'collector.tool.extract', 'tool_name': 'web.extract', 'ok': extracted.ok}
                )
                sanitized = str(extracted.output.get('sanitized', ''))
                extracted_fields = extracted.output.get('extract_fields', {})
                retrieval_status = 'partial'
                snippet = search_snippet[:500] if search_snippet else ''
                confidence = 0.55

            h = content_hash(sanitized)
            local_path = self._persist_raw_content(run_id=run_id, evidence_hash=h, content=sanitized)
            captured = datetime.now(UTC)

            return {
                'query': query,
                'title': search_title,
                'source_url': source_url,
                'snippet': snippet,
                'source_provider': source_provider,
                'source_type': self._infer_source_type(source_url),
                'retrieval_method': f'{source_provider}+{fetch_provider}',
                'retrieval_status': retrieval_status,
                'captured_at': captured,
                'extract_fields': extracted_fields,
                'confidence': confidence,
                'recency_score': recency_score(captured),
                'license_or_tos_note': 'public web source, compliance checkpoint recorded',
                'raw_content_path': str(local_path),
                'content_hash': h,
                'content_excerpt': sanitized[:1000],
                'latency_ms': 0,
                'error_code': '' if content and len(content) > 100 else 'fetch_fallback_partial',
                'schema_field': field_name,
                'query_template': query,
                'recommended_source_type': ','.join(recommended_sources),
            }

        if fetch_tasks:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(fetch_tasks), 4)) as executor:
                futures = [executor.submit(_fetch_one, task) for task in fetch_tasks]
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    if result:
                        candidate_rows.append(result)

        candidate_rows = self._rerank_pricing_candidates(candidate_rows, output, competitor, per_field_limit)
        candidate_rows = dedup_by_url_and_hash(candidate_rows)
        candidate_rows = verify_cross_source(candidate_rows)
        if max_items is not None:
            candidate_rows = candidate_rows[:max_items]
        output.evidences = candidate_rows
        output.provider_events.append({'event_type': 'collector.fallback.trace', 'fallback_trace': fallback_trace})
        return output

    def _prioritize_search_results(
        self,
        results: list[tuple[str, str, str, list[str], str, str, str]],
        output: CollectorOutput,
        competitor: str,
    ) -> list[tuple[str, str, str, list[str], str, str, str]]:
        prioritized: list[tuple[str, str, str, list[str], str, str, str]] = []
        pricing_results: list[tuple[int, tuple[str, str, str, list[str], str, str, str]]] = []

        for result in results:
            field_name = result[0]
            if field_name != 'pricing_model':
                prioritized.append(result)
                continue
            score = self._score_pricing_result(result[2], result[4], result[5], result[6])
            pricing_results.append((score, result))

        if not pricing_results:
            return prioritized

        pricing_results.sort(key=lambda item: item[0], reverse=True)
        positive_results = [result for score, result in pricing_results if score > 0]
        candidate_results = positive_results if positive_results else [result for _, result in pricing_results]
        selected_results = self._diversify_pricing_results(candidate_results)
        top_score = pricing_results[0][0]

        output.provider_events.append(
            {
                'event_type': 'collector.pricing_results.prioritized',
                'competitor': competitor,
                'field_name': 'pricing_model',
                'candidate_count': len(pricing_results),
                'selected_count': len(selected_results),
                'top_score': top_score,
            }
        )
        prioritized.extend(selected_results)
        return prioritized

    def _diversify_pricing_results(
        self,
        results: list[tuple[str, str, str, list[str], str, str, str]],
    ) -> list[tuple[str, str, str, list[str], str, str, str]]:
        diversified: list[tuple[str, str, str, list[str], str, str, str]] = []
        seen_hosts: set[str] = set()

        for result in results:
            host = self._extract_host(result[2])
            if host and host in seen_hosts:
                continue
            diversified.append(result)
            if host:
                seen_hosts.add(host)

        if diversified:
            return diversified
        return results

    @staticmethod
    def _extract_host(url: str) -> str:
        try:
            return urlparse(url).netloc.casefold()
        except Exception:
            return ''

    def _score_pricing_result(
        self,
        source_url: str,
        title: str,
        snippet: str,
        source_provider: str,
    ) -> int:
        haystack = ' '.join([source_url, title, snippet]).lower()
        score = 0

        strong_terms = ['pricing', 'price', 'plans', 'plan', 'billing', 'subscription', '套餐', '价格', '定价', '计费', '版本', '收费']
        weak_terms = ['edition', '购买', '升级', 'pro', 'enterprise', '企业版', '免费版']
        discussion_terms = ['知乎', 'question', 'answer', '对比', '测评', '评测', '哪个好', '性价比', 'vs']
        community_hosts = ['zhihu.com', 'xiaohongshu.com', 'weibo.com', 'tieba.baidu.com']

        if any(term in haystack for term in strong_terms):
            score += 4
        if any(term in haystack for term in weak_terms):
            score += 2
        if any(host in source_url.lower() for host in community_hosts):
            score -= 4
        if source_provider == 'zhihu_official':
            score -= 3
        if any(term.lower() in haystack for term in discussion_terms):
            score -= 2
        if any(token in source_url.lower() for token in ['/pricing', '/price', '/plans', '/billing', '/buy', '/order', '/edition']):
            score += 4
        return score

    @staticmethod
    def _field_prefetch_limit(*, field_name: str, per_field_limit: int) -> int:
        if field_name == 'pricing_model':
            return max(per_field_limit * 3, 6)
        return per_field_limit

    def _rerank_pricing_candidates(
        self,
        candidate_rows: list[dict[str, Any]],
        output: CollectorOutput,
        competitor: str,
        per_field_limit: int,
    ) -> list[dict[str, Any]]:
        pricing_rows: list[tuple[int, dict[str, Any]]] = []
        other_rows: list[dict[str, Any]] = []
        for row in candidate_rows:
            if str(row.get('schema_field', '')).strip() != 'pricing_model':
                other_rows.append(row)
                continue
            score = self._score_pricing_content(row)
            pricing_rows.append((score, row))

        if not pricing_rows:
            return candidate_rows

        pricing_rows.sort(key=lambda item: item[0], reverse=True)
        ranked_rows = [row for _, row in pricing_rows]
        top_scores = [score for score, _ in pricing_rows[: min(len(pricing_rows), 5)]]
        output.provider_events.append(
            {
                'event_type': 'collector.pricing_content_reranked',
                'competitor': competitor,
                'field_name': 'pricing_model',
                'candidate_count': len(pricing_rows),
                'selected_count': min(len(pricing_rows), per_field_limit),
                'top_scores': top_scores,
            }
        )
        return ranked_rows[:per_field_limit] + other_rows

    def _score_pricing_content(self, row: dict[str, Any]) -> int:
        text = ' '.join(
            [
                str(row.get('title', '') or ''),
                str(row.get('snippet', '') or ''),
                str(row.get('content_excerpt', '') or ''),
                str(row.get('query', '') or ''),
            ]
        ).lower()
        score = 0

        exact_patterns = [
            r'\d+\s*元\s*/\s*月',
            r'\d+\s*元\s*/\s*年',
            r'\d+\s*元\s*/\s*人\s*/\s*月',
            r'\d+\s*元\s*/\s*人\s*/\s*年',
            r'￥\s*\d+',
            r'¥\s*\d+',
        ]
        for pattern in exact_patterns:
            matches = re.findall(pattern, text)
            score += len(matches) * 8

        strong_terms = ['元/月', '元/年', '每人', '每月', '每年', '席位', 'seat', 'license', '报价', '价格', '定价', '套餐', '计费']
        medium_terms = ['免费', '试用', '商业版', '企业版', '专业版', '基础版', '标准版', '人数', '应用数', '额度']
        weak_negative_terms = ['测评', '体验', '对比', '哪个好', '性价比', '社区', '博客']

        score += sum(3 for term in strong_terms if term in text)
        score += sum(1 for term in medium_terms if term in text)
        score -= sum(2 for term in weak_negative_terms if term in text)

        if any(token in str(row.get('source_url', '')).lower() for token in ['/pricing', '/price', '/plans', '/billing', '/service']):
            score += 3
        return score

    def _resolve_schema_fields(
        self,
        *,
        competitor: str,
        industry: str,
        schema_plan: list[AnalysisSchemaField] | list[dict] | None,
        field_query_overrides: dict[str, list[str]] | None = None,
    ) -> list[dict]:
        if not schema_plan:
            return [
                {'field_name': '默认', 'queries': build_queries(competitor, industry), 'recommended_sources': ['public_web']},
            ]
        output: list[dict] = []
        current_year = datetime.now(UTC).year
        for item in schema_plan:
            if isinstance(item, AnalysisSchemaField):
                raw_item = item.model_dump(mode='json')
            elif isinstance(item, dict):
                raw_item = item
            else:
                continue
            field_name = self._repair_mojibake(str(raw_item.get('field_name', '')).strip())
            if not field_name:
                continue
            templates = raw_item.get('query_templates', [])
            sources = raw_item.get('recommended_sources', ['public_web'])
            override_key = f'{competitor}::{field_name}'
            override_queries = (field_query_overrides or {}).get(override_key, [])
            if override_queries:
                queries = [str(q).strip() for q in override_queries if str(q).strip()][:4]
            else:
                queries = [qt.format(product=competitor, current_year=current_year) for qt in templates]
            output.append({'field_name': field_name, 'queries': queries, 'recommended_sources': sources})
        return output

    def _run_search_phase(
        self,
        *,
        query: str,
        output: CollectorOutput,
        fallback_trace: list[dict],
        provider_allowlist: list[str] | None,
        field_name: str,
        strategy: str,
    ) -> list[SearchHit]:
        max_results = self._search_max_results(field_name=field_name)
        result = self.tool_router.invoke(
            ToolRequest(
                name='web.search',
                args={
                    'query': query,
                    'provider_allowlist': provider_allowlist,
                    'max_results': max_results,
                },
                metadata={'group': 'web'},
            )
        )
        raw_hits = result.output.get('hits', []) if result.ok else []
        hits = self._coerce_search_hits(raw_hits, query=query)
        trace = result.output.get('trace', []) if result.ok else []
        output.tool_events.append({'event_type': 'collector.tool.search', 'tool_name': 'web.search', 'ok': result.ok})
        if provider_allowlist:
            output.provider_events.append(
                {
                    'event_type': 'collector.search.strategy',
                    'query': query,
                    'field_name': field_name,
                    'strategy': strategy,
                    'provider_allowlist': provider_allowlist,
                    'max_results': max_results,
                }
            )
        fallback_trace.extend(trace)
        for hit in hits:
            output.provider_events.append({
                'event_type': 'collector.search.hit',
                'query': query,
                'url': hit.url,
                'title': hit.title,
                'snippet': hit.snippet,
                'source_provider': hit.source_provider,
                'field_name': field_name,
                'strategy': strategy,
                'max_results': max_results,
            })
        return hits

    def _search_max_results(self, *, field_name: str) -> int:
        if field_name == 'pricing_model':
            return 8
        return 4

    @staticmethod
    def _coerce_search_hits(raw_hits: Any, *, query: str) -> list[SearchHit]:
        if not isinstance(raw_hits, list):
            return []
        normalized: list[SearchHit] = []
        for item in raw_hits:
            if isinstance(item, SearchHit):
                normalized.append(item)
                continue
            if isinstance(item, dict):
                normalized.append(
                    SearchHit(
                        query=str(item.get('query', '') or query),
                        title=str(item.get('title', '') or ''),
                        url=str(item.get('url', '') or ''),
                        snippet=str(item.get('snippet', '') or ''),
                        source_provider=str(item.get('source_provider', '') or ''),
                        status=str(item.get('status', 'ok') or 'ok'),
                        latency_ms=int(item.get('latency_ms', 0) or 0),
                    )
                )
                continue
            normalized.append(
                SearchHit(
                    query=str(getattr(item, 'query', '') or query),
                    title=str(getattr(item, 'title', '') or ''),
                    url=str(getattr(item, 'url', '') or ''),
                    snippet=str(getattr(item, 'snippet', '') or ''),
                    source_provider=str(getattr(item, 'source_provider', '') or ''),
                    status=str(getattr(item, 'status', 'ok') or 'ok'),
                    latency_ms=int(getattr(item, 'latency_ms', 0) or 0),
                )
            )
        return normalized

    def _run_fetch_phase(
        self,
        *,
        url: str,
        output: CollectorOutput,
        fallback_trace: list[dict],
        provider_order: list[str],
        field_name: str,
        strategy: str,
    ) -> tuple[str, str]:
        result = self.tool_router.invoke(
            ToolRequest(
                name='web.fetch',
                args={'url': url, 'provider_order': provider_order},
                metadata={'group': 'web'},
            )
        )
        trace = result.output.get('trace', []) if result.ok else []
        content = str(result.output.get('content', '')) if result.ok else ''
        provider_name = result.provider if result.ok else ''
        output.tool_events.append({'event_type': 'collector.tool.fetch', 'tool_name': 'web.fetch', 'ok': result.ok})

        fallback_trace.extend(trace)
        output.provider_events.append({
            'event_type': 'collector.fetch.result',
            'url': url,
            'provider': provider_name,
            'content_length': len(content) if content else 0,
            'field_name': field_name,
            'strategy': strategy,
        })
        return content, provider_name

    def _persist_raw_content(self, run_id: str, evidence_hash: str, content: str) -> Path:
        # Persist under project/.data/collector_raw to avoid cwd-dependent duplicate directories.
        project_root = Path(__file__).resolve().parents[4]
        bucket = 'preview' if run_id.strip() == 'preview' else run_id.strip()
        base_path = project_root / '.data' / 'collector_raw' / bucket
        base_path.mkdir(parents=True, exist_ok=True)
        file_path = base_path / f'{evidence_hash}.txt'
        file_path.write_text(content, encoding='utf-8')
        try:
            return file_path.relative_to(project_root)
        except ValueError:
            return file_path

    def _infer_source_type(self, url: str) -> str:
        u = url.lower()
        if any(x in u for x in ['reddit.com', 'news.ycombinator.com', 'zhihu.com']):
            return 'community'
        if any(x in u for x in ['g2.com', 'capterra.com', 'trustpilot.com']):
            return 'review'
        if any(x in u for x in ['news.', '.news', 'blog.', '.blog']):
            return 'news'
        if any(x in u for x in ['report.', '.report', 'whitepaper', 'research.']):
            return 'report'
        return 'official'

    @staticmethod
    def _repair_mojibake(text: str) -> str:
        return text
