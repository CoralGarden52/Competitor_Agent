from __future__ import annotations

import concurrent.futures
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.collector.extractor import extract_fields, mask_pii
from app.core.collector.normalizer import content_hash, normalize_url, recency_score
from app.core.collector.provider_registry import ProviderRegistry
from app.core.collector.providers import build_fetch_provider_catalog, build_search_provider_catalog
from app.core.collector.query_planner import build_queries
from app.core.collector.types import CollectorOutput, SearchHit
from app.core.collector.verifier import dedup_by_url_and_hash, verify_cross_source
from app.core.config import AppConfig
from app.core.models import AnalysisSchemaField
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
        schema_plan: list[AnalysisSchemaField] | list[dict] | None = None,
        per_field_limit: int = 3,
    ) -> CollectorOutput:
        output = CollectorOutput()
        fields = self._resolve_schema_fields(competitor=competitor, industry=industry, schema_plan=schema_plan)
        candidate_rows: list[dict] = []
        fallback_trace: list[dict] = []
        max_items = max_urls if max_urls is not None and max_urls > 0 else None

        # 阶段1: 并行执行搜索，收集所有需要抓取的任务
        # 修改: 保存搜索结果的标题和摘要 (field_name, query, url, recommended_sources, title, snippet, source_provider)
        fetch_tasks: list[tuple[str, str, str, list[str], str, str, str]] = []
        
        # 为每个字段创建搜索任务
        search_tasks: list[tuple[str, str, list[str], int]] = []
        
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
                search_tasks.append((field_name, query, recommended_sources, per_field_limit))
        
        # 并行执行搜索
        # 修改: 返回包含标题和摘要的搜索结果
        def _search_one(task: tuple[str, str, list[str], int]) -> list[tuple[str, str, str, list[str], str, str, str]]:
            field_name, query, recommended_sources, _ = task
            local_fallback_trace: list[dict] = []
            search_hits = self._run_search_phase(query=query, output=output, fallback_trace=local_fallback_trace)
            fallback_trace.extend(local_fallback_trace)
            
            # 修改: 保存标题、摘要和来源
            results: list[tuple[str, str, str, list[str], str, str, str]] = []
            for hit in search_hits:
                source_url = normalize_url(hit.url)
                results.append((field_name, hit.query, source_url, recommended_sources, hit.title, hit.snippet, hit.source_provider))
            return results
        
        all_search_results: list[tuple[str, str, str, list[str], str, str, str]] = []
        if search_tasks:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(search_tasks), 3)) as executor:
                futures = [executor.submit(_search_one, task) for task in search_tasks]
                for future in concurrent.futures.as_completed(futures):
                    all_search_results.extend(future.result())
        
        # 按字段限制整理结果
        field_hit_counts: dict[str, int] = {}
        for field_name, query, source_url, recommended_sources, title, snippet, source_provider in all_search_results:
            if max_items is not None and len(fetch_tasks) >= max_items:
                break
            current_hits = field_hit_counts.get(field_name, 0)
            if current_hits >= per_field_limit:
                output.provider_events.append(
                    {
                        'event_type': 'collector.field_quota_reached',
                        'competitor': competitor,
                        'field_name': field_name,
                        'limit': per_field_limit,
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

        # 阶段2: 并行执行抓取
        # 修改: 使用保存的标题和摘要，并针对字段类型提取相关信息
        def _fetch_one(task: tuple[str, str, str, list[str], str, str, str]) -> dict | None:
            field_name, query, source_url, recommended_sources, search_title, search_snippet, source_provider = task
            local_fallback_trace: list[dict] = []
            content, fetch_provider = self._run_fetch_phase(url=source_url, output=output, fallback_trace=local_fallback_trace)
            fallback_trace.extend(local_fallback_trace)
            
            from datetime import datetime, UTC
            
            # 修改: 使用搜索结果的标题和摘要作为 fallback
            # 如果抓取成功，优先使用抓取的内容；否则使用搜索结果的标题和摘要
            if content and len(content) > 100:
                # 抓取成功，使用抓取的内容
                sanitized = mask_pii(content)
                extracted_fields = extract_fields(sanitized, search_snippet)
                h = content_hash(sanitized)
                local_path = self._persist_raw_content(run_id=run_id, evidence_hash=h, content=sanitized)
                retrieval_status = 'ok'
                title = search_title  # 仍使用搜索结果的标题
                snippet = sanitized[:500]  # 使用抓取内容的前500字符作为摘要
                confidence = 0.72
            else:
                # 抓取失败，使用搜索结果的标题和摘要
                sanitized = mask_pii(search_snippet)
                extracted_fields = extract_fields(sanitized, search_snippet)
                h = content_hash(sanitized)
                local_path = self._persist_raw_content(run_id=run_id, evidence_hash=h, content=sanitized)
                retrieval_status = 'partial'
                title = search_title
                snippet = search_snippet[:500] if search_snippet else ''
                confidence = 0.55
            
            captured = datetime.now(UTC)
            
            return {
                'query': query,
                'title': title,
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

        candidate_rows = dedup_by_url_and_hash(candidate_rows)
        candidate_rows = verify_cross_source(candidate_rows)
        if max_items is not None:
            candidate_rows = candidate_rows[:max_items]
        output.evidences = candidate_rows
        output.provider_events.append({'event_type': 'collector.fallback.trace', 'fallback_trace': fallback_trace})
        return output

    def _resolve_schema_fields(self, *, competitor: str, industry: str, schema_plan: list[AnalysisSchemaField] | list[dict] | None) -> list[dict]:
        if not schema_plan:
            return [
                {'field_name': '默认', 'queries': build_queries(competitor, industry), 'recommended_sources': ['public_web']},
            ]
        output: list[dict] = []
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
            queries = [qt.format(product=competitor) for qt in templates]
            output.append({'field_name': field_name, 'queries': queries, 'recommended_sources': sources})
        return output

    def _run_search_phase(self, query: str, output: CollectorOutput, fallback_trace: list[dict]) -> list[SearchHit]:
        from app.core.collector.search import search_with_fallback
        hits, trace = search_with_fallback(
            query=query,
            registry=self.registry,
            fallback_trace=fallback_trace,
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
            })
        return hits

    def _run_fetch_phase(self, url: str, output: CollectorOutput, fallback_trace: list[dict]) -> tuple[str, str]:
        from app.core.collector.fetch import fetch_with_fallback
        content, provider, trace = fetch_with_fallback(
            url=url,
            registry=self.registry,
            fallback_trace=fallback_trace,
        )
        fallback_trace.extend(trace)
        output.provider_events.append({
            'event_type': 'collector.fetch.result',
            'url': url,
            'provider': provider,
            'content_length': len(content) if content else 0,
        })
        return content, provider

    def _persist_raw_content(self, run_id: str, evidence_hash: str, content: str) -> Path:
        # 使用预览保存目录作为默认路径
        base_path = Path(getattr(self.config, 'collector_raw_content_path', 'collector_raw'))
        base_path.mkdir(parents=True, exist_ok=True)
        file_path = base_path / f'{run_id}_{evidence_hash}.txt'
        file_path.write_text(content, encoding='utf-8')
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
