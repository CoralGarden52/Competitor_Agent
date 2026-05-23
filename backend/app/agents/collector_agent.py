from __future__ import annotations

from app.core.collector import CollectorPipeline
from app.core.config import get_config
from app.core.models import CollectOutput, RawEvidence, RunState
from app.core.storage import SQLiteStore


class CollectorAgent:
    def __init__(self, pipeline: CollectorPipeline, store: SQLiteStore):
        self.pipeline = pipeline
        self.store = store
        self.config = get_config()

    def run(self, state: RunState) -> CollectOutput:
        out = CollectOutput()
        active_competitors = state.planned_competitors or state.competitors
        for competitor in active_competitors:
            result = self.pipeline.collect(
                run_id=state.run_id,
                industry=state.industry,
                competitor=competitor,
                schema_plan=state.analysis_schema_plan,
                per_field_limit=self.config.collector_per_field_limit,
            )
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
