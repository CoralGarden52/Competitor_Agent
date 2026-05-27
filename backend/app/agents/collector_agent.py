from __future__ import annotations

import concurrent.futures

from app.core.collector import CollectorPipeline
from app.core.config import get_config
from app.core.models import CollectOutput, RawEvidence, RunState
from app.core.storage import SQLiteStore


class CollectorAgent:
    def __init__(self, pipeline: CollectorPipeline, store: SQLiteStore):
        self.pipeline = pipeline
        self.store = store
        self.config = get_config()

    def run(
        self,
        state: RunState,
        *,
        target_competitors: list[str] | None = None,
        field_query_overrides: dict[str, list[str]] | None = None,
    ) -> CollectOutput:
        out = CollectOutput()
        active_competitors = target_competitors or state.planned_competitors or state.competitors
        
        if not active_competitors:
            return out
        
        # 定义单个竞品的采集函数
        def _collect_one(competitor: str):
            print(f"[{__import__('time').strftime('%H:%M:%S')}] 开始采集: {competitor}")
            start = __import__('time').time()
            
            result = self.pipeline.collect(
                run_id=state.run_id,
                industry=state.industry,
                competitor=competitor,
                schema_plan=state.analysis_schema_plan,
                per_field_limit=self.config.collector_per_field_limit,
                field_query_overrides=field_query_overrides,
            )
            
            elapsed = __import__('time').time() - start
            print(f"[{__import__('time').strftime('%H:%M:%S')}] 完成采集: {competitor} (耗时={elapsed:.2f}s, 证据数={len(result.evidences)})")
            
            return competitor, result
        
        # 并发执行所有竞品的采集
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(active_competitors), 4)) as executor:
            futures = {executor.submit(_collect_one, comp): comp for comp in active_competitors}
            
            for future in concurrent.futures.as_completed(futures):
                competitor, result = future.result()
                
                out.errors.extend(result.errors)
                out.provider_events.extend(result.provider_events)
                
                for item in result.evidences:
                    ev = RawEvidence(
                        query=item.get('query', ''),
                        source_url=item.get('source_url', ''),
                        title=item.get('title', ''),
                        snippet=item.get('snippet', f'No snippet extracted for {competitor}'),
                        claim_tags=['feature', 'pricing', 'feedback'],
                        credibility_score=item.get('confidence', 0.7),
                        confidence=item.get('confidence', 0.7),
                        recency_score=item.get('recency_score', 0.5),
                        raw_content_path=item.get('raw_content_path', ''),
                        extract_fields=item.get('extract_fields', {}),
                        license_or_tos_note=item.get('license_or_tos_note', ''),
                        source_type=item.get('source_type', 'report'),
                        retrieval_method=item.get('retrieval_method', 'collector_pipeline'),
                        retrieval_status=item.get('retrieval_status', 'partial'),
                        domain_extensions={
                            'competitor': competitor,
                            'source_provider': item.get('source_provider', ''),
                            'content_excerpt': item.get('content_excerpt', ''),
                            'schema_field': item.get('schema_field', ''),
                            'query_template': item.get('query_template', ''),
                            'recommended_source_type': item.get('recommended_source_type', ''),
                        },
                    )
                    out.raw_evidences.append(ev)
                    if item.get('content_hash') and ev.raw_content_path:
                        self.store.index_raw_evidence_content(
                            run_id=state.run_id,
                            evidence_id=ev.evidence_id,
                            source_url=ev.source_url,
                            content_hash=item['content_hash'],
                            local_path=ev.raw_content_path,
                        )
        
        return out
