from __future__ import annotations

import concurrent.futures

from app.core.collector import CollectorPipeline
from app.core.collector.deep_dive import CollectorDeepDiveCoordinator
from app.core.config import get_config
from app.core.models import CollectOutput, RawEvidence, RunState, StageName, TaskEnvelope, TaskResult
from app.core.storage import PostgresStore


class CollectorAgent:
    def __init__(self, pipeline: CollectorPipeline, store: PostgresStore, deep_dive: CollectorDeepDiveCoordinator | None = None):
        self.pipeline = pipeline
        self.store = store
        self.deep_dive = deep_dive
        self.config = get_config()

    def run(
        self,
        state: RunState,
        *,
        target_competitors: list[str] | None = None,
        field_query_overrides: dict[str, list[str]] | None = None,
        enable_deep_dive: bool = True,
    ) -> CollectOutput:
        out = CollectOutput()
        active_competitors = target_competitors or state.planned_competitors or state.competitors
        
        if not active_competitors:
            return out
        collected_rows: list[dict] = []
        
        # 定义单个竞品的采集函数
        def _collect_one(competitor: str):
            print(f"[{__import__('time').strftime('%H:%M:%S')}] 开始采集: {competitor}")
            start = __import__('time').time()
            self.store.append_stage_event(
                state.run_id,
                StageName.collect,
                'collector.competitor.started',
                {'competitor': competitor},
            )
            
            result = self.pipeline.collect(
                run_id=state.run_id,
                industry=state.industry,
                competitor=competitor,
                schema_plan=state.analysis_schema_plan,
                per_field_limit=self.config.collector_per_field_limit,
                field_query_overrides=field_query_overrides,
                target_fields=self._target_fields_for(competitor, field_query_overrides),
            )
            
            elapsed = __import__('time').time() - start
            print(f"[{__import__('time').strftime('%H:%M:%S')}] 完成采集: {competitor} (耗时={elapsed:.2f}s, 证据数={len(result.evidences)})")
            self.store.append_stage_event(
                state.run_id,
                StageName.collect,
                'collector.competitor.completed',
                {
                    'competitor': competitor,
                    'elapsed_sec': round(float(elapsed), 2),
                    'evidence_count': len(result.evidences),
                },
            )
            
            return competitor, result
        
        # 并发执行所有竞品的采集
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(active_competitors), 4)) as executor:
            futures = {executor.submit(_collect_one, comp): comp for comp in active_competitors}
            
            for future in concurrent.futures.as_completed(futures):
                competitor, result = future.result()
                
                out.errors.extend(result.errors)
                out.provider_events.extend(result.provider_events)
                out.tool_events.extend(result.tool_events)
                for item in result.evidences:
                    item['competitor'] = competitor
                    collected_rows.append(item)

        if enable_deep_dive and self.deep_dive is not None:
            deep_result = self.deep_dive.enrich(
                run_id=state.run_id,
                attempt=state.attempt,
                industry=state.industry,
                competitors=list(active_competitors),
                schema_plan=state.analysis_schema_plan,
                evidences=collected_rows,
                field_query_overrides=field_query_overrides,
            )
            collected_rows = deep_result.evidences
            out.provider_events.extend(deep_result.provider_events)
            out.errors.extend(deep_result.errors)

        for item in collected_rows:
            competitor = str(item.get('competitor', '') or '')
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
                    'pricing_capture': item.get('pricing_capture', {}),
                    'subagent_id': item.get('subagent_id', ''),
                    'verification_status': item.get('verification_status', ''),
                    'verification_claims': item.get('verification_claims', []),
                    'verification_conflicts': item.get('verification_conflicts', []),
                    'verification_gaps': item.get('verification_gaps', []),
                    'source_host_count': item.get('source_host_count', 0),
                    'cross_source_ok': item.get('cross_source_ok', False),
                    'risk_flag': item.get('risk_flag', False),
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

    def consume_task(self, task: TaskEnvelope, state: RunState) -> tuple[TaskResult, CollectOutput]:
        payload = task.input_payload if isinstance(task.input_payload, dict) else {}
        target_competitors = payload.get('target_competitors')
        if not isinstance(target_competitors, list):
            target_competitors = None
        field_query_overrides = payload.get('field_query_overrides')
        if not isinstance(field_query_overrides, dict):
            field_query_overrides = None
        result = self.run(
            state,
            target_competitors=[str(item).strip() for item in target_competitors if str(item).strip()] if target_competitors else None,
            field_query_overrides=field_query_overrides,
        )
        active_competitors = target_competitors or state.planned_competitors or state.competitors
        task_result = TaskResult(
            task_id=task.task_id,
            run_id=task.run_id,
            owner_agent='CollectorAgent',
            status='completed',
            summary=f'collected {len(result.raw_evidences)} evidences for {len(active_competitors)} competitors',
            output_payload={
                'evidence_count': len(result.raw_evidences),
                'error_count': len(result.errors),
                'target_competitors': active_competitors,
            },
            changed_fields=[str(item.get('schema_field', '')).strip() for item in [ev.domain_extensions for ev in result.raw_evidences] if isinstance(item, dict) and str(item.get('schema_field', '')).strip()],
            next_recommendations=['analyze_evidence'] if result.raw_evidences else ['collect_evidence'],
        )
        return task_result, result

    @staticmethod
    def _target_fields_for(competitor: str, overrides: dict[str, list[str]] | None) -> set[str] | None:
        if not overrides:
            return None
        prefix = f'{competitor}::'
        fields = {key[len(prefix):] for key in overrides if key.startswith(prefix) and key[len(prefix):]}
        return fields or None
