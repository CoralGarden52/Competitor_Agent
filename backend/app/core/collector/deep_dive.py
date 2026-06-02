from __future__ import annotations

import concurrent.futures
import contextvars
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app.core.config import AppConfig
from app.core.models import AnalysisSchemaField
from harness.subagents import SubagentBudget, SubagentExecutor, SubagentRequest, collector_deep_dive_role
from harness.subagents.tracing import subagent_trace


@dataclass
class DeepDiveOutput:
    evidences: list[dict[str, Any]] = field(default_factory=list)
    provider_events: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class CollectorDeepDiveCoordinator:
    def __init__(self, *, executor: SubagentExecutor, config: AppConfig) -> None:
        self.executor = executor
        self.config = config

    def enrich(
        self,
        *,
        run_id: str,
        attempt: int,
        industry: str,
        competitors: list[str],
        schema_plan: list[AnalysisSchemaField] | list[dict],
        evidences: list[dict[str, Any]],
        field_query_overrides: dict[str, list[str]] | None = None,
    ) -> DeepDiveOutput:
        if not self.config.subagent_enabled:
            return DeepDiveOutput(evidences=evidences)
        tasks = self._build_tasks(
            run_id=run_id,
            attempt=attempt,
            industry=industry,
            competitors=competitors,
            schema_plan=schema_plan,
            evidences=evidences,
            field_query_overrides=field_query_overrides or {},
        )[: self.config.subagent_max_tasks_per_collect]
        if not tasks:
            return DeepDiveOutput(evidences=evidences)

        budget = SubagentBudget(
            max_rounds=self.config.subagent_max_rounds,
            max_tool_calls=self.config.subagent_max_tool_calls,
            max_tokens=self.config.subagent_max_tokens,
            timeout_s=float(self.config.subagent_timeout_seconds),
        )
        results = []
        with subagent_trace(
            name='collector.deep_dive',
            run_type='chain',
            inputs={'run_id': run_id, 'attempt': attempt, 'task_count': len(tasks)},
            metadata={'parent_run_id': run_id, 'attempt': attempt, 'task_count': len(tasks)},
        ):
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(len(tasks), self.config.subagent_max_concurrency)
            ) as pool:
                futures = [
                    pool.submit(
                        contextvars.copy_context().run,
                        self.executor.run,
                        request=task,
                        role=collector_deep_dive_role(),
                        budget=budget,
                    )
                    for task in tasks
                ]
                for future in concurrent.futures.as_completed(futures):
                    results.append(future.result())

        merged = list(evidences)
        events: list[dict[str, Any]] = []
        errors: list[str] = []
        for result in results:
            annotation = {
                'subagent_id': result.subagent_id,
                'verification_status': self._verification_status(result),
                'verification_claims': result.verification_claims,
                'verification_conflicts': result.verification_conflicts,
                'verification_gaps': result.verification_gaps,
            }
            for item in merged:
                if self._matches(item, result.competitor, result.field_name):
                    item.update(annotation)
            for item in result.new_evidences:
                item.update(annotation)
                item['competitor'] = result.competitor
                merged.append(item)
            events.append(
                {
                    'event_type': 'collector.subagent.completed',
                    'subagent_id': result.subagent_id,
                    'competitor': result.competitor,
                    'field_name': result.field_name,
                    'status': result.status,
                    'budget_used': result.usage.__dict__,
                }
            )
            if result.status != 'completed':
                errors.append(f'{result.competitor}:{result.field_name}: {result.error or result.status}')
        self._refresh_source_host_counts(merged)
        return DeepDiveOutput(evidences=self._dedupe(merged), provider_events=events, errors=errors)

    def _build_tasks(
        self,
        *,
        run_id: str,
        attempt: int,
        industry: str,
        competitors: list[str],
        schema_plan: list[AnalysisSchemaField] | list[dict],
        evidences: list[dict[str, Any]],
        field_query_overrides: dict[str, list[str]],
    ) -> list[SubagentRequest]:
        tasks: list[tuple[int, int, SubagentRequest]] = []
        for competitor in competitors:
            for priority, field_name in self._schema_fields(schema_plan):
                key = f'{competitor}::{field_name}'
                matches = [item for item in evidences if self._matches(item, competitor, field_name)]
                qa_queries = field_query_overrides.get(key, [])
                if field_query_overrides and not qa_queries:
                    continue
                if not qa_queries and self._source_host_count(matches) >= 2:
                    continue
                tasks.append(
                    (
                        0 if qa_queries else 1,
                        priority,
                        SubagentRequest(
                            parent_run_id=run_id,
                            attempt=attempt,
                            industry=industry,
                            competitor=competitor,
                            field_name=field_name,
                            objective=(
                                f'调查竞品 {competitor} 的字段 {field_name}。寻找相互独立的公开来源，'
                                '交叉核验已确认事实，并报告冲突或仍然存在的信息缺口。'
                            ),
                            seed_queries=list(qa_queries),
                            existing_evidences=[self._evidence_summary(item) for item in matches[:4]],
                        ),
                    )
                )
        tasks.sort(key=lambda item: (item[0], item[1], item[2].competitor, item[2].field_name))
        return [item[2] for item in tasks]

    @staticmethod
    def _schema_fields(schema_plan: list[AnalysisSchemaField] | list[dict]) -> list[tuple[int, str]]:
        output = []
        for item in schema_plan:
            payload = item.model_dump(mode='json') if isinstance(item, AnalysisSchemaField) else item
            if not isinstance(payload, dict):
                continue
            field_name = str(payload.get('field_name', '') or '').strip()
            if field_name:
                output.append((int(payload.get('priority', 1) or 1), field_name))
        return output

    @staticmethod
    def _matches(item: dict[str, Any], competitor: str, field_name: str) -> bool:
        return (
            str(item.get('competitor', '') or '').strip().casefold() == competitor.casefold()
            and str(item.get('schema_field', '') or '').strip().casefold() == field_name.casefold()
        )

    @staticmethod
    def _source_host_count(items: list[dict[str, Any]]) -> int:
        return len(
            {
                urlparse(str(item.get('source_url', '') or '')).netloc.casefold()
                for item in items
                if urlparse(str(item.get('source_url', '') or '')).netloc
            }
        )

    @classmethod
    def _refresh_source_host_counts(cls, items: list[dict[str, Any]]) -> None:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in items:
            key = (str(item.get('competitor', '') or ''), str(item.get('schema_field', '') or ''))
            groups.setdefault(key, []).append(item)
        for rows in groups.values():
            host_count = cls._source_host_count(rows)
            for item in rows:
                item['source_host_count'] = host_count
                item['cross_source_ok'] = host_count >= 2
                if host_count < 2:
                    item['risk_flag'] = True

    @staticmethod
    def _verification_status(result) -> str:
        if result.verification_conflicts:
            return 'conflicting'
        if result.verification_claims and not result.verification_gaps:
            return 'supported'
        return 'insufficient'

    @staticmethod
    def _evidence_summary(item: dict[str, Any]) -> dict[str, Any]:
        return {
            'source_url': item.get('source_url', ''),
            'title': item.get('title', ''),
            'snippet': item.get('snippet', ''),
            'source_type': item.get('source_type', ''),
        }

    @staticmethod
    def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output = []
        seen = set()
        for item in items:
            key = str(item.get('source_url', '') or ''), str(item.get('schema_field', '') or '')
            if key in seen:
                continue
            seen.add(key)
            output.append(item)
        return output
