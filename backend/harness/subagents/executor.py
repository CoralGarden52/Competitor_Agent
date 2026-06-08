from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from app.core.agent_llm import AgentLLMClient
from app.core.models import StageName
from app.core.storage import SQLiteStore
from harness.tools import ToolLoopError, ToolLoopExecutor, ToolRouter
from harness.subagents.registry import SubagentRole
from harness.subagents.tracing import finish_subagent_trace, subagent_trace
from harness.subagents.types import (
    SubagentBudget,
    SubagentBudgetExceeded,
    SubagentRequest,
    SubagentResult,
    SubagentTokenTracker,
    SubagentUsage,
)


class SubagentExecutor:
    def __init__(self, *, llm: AgentLLMClient, tool_router: ToolRouter, store: SQLiteStore) -> None:
        self.llm = llm
        self.tool_router = tool_router
        self.store = store

    def run(self, *, request: SubagentRequest, role: SubagentRole, budget: SubagentBudget) -> SubagentResult:
        started = time.monotonic()
        usage = SubagentUsage()
        token_tracker = SubagentTokenTracker(budget.max_tokens)
        history: list[dict[str, Any]] = []
        observed_sources: dict[str, dict[str, Any]] = {}
        final_output: dict[str, Any] = {}
        status = 'completed'
        error = ''
        metadata = self._metadata(request, budget)
        self._save_event(request, 'subagent.started', {'budget_limit': metadata['budget_limit']})
        self.store.save_subagent_run(request=request, budget=budget, status='running')

        try:
            with subagent_trace(
                name=f'subagent.{request.competitor}.{request.field_name}',
                run_type='chain',
                inputs=self._task_payload(request),
                metadata=metadata,
            ) as subagent_span:
                def _after_tool(tool_name: str, arguments: dict[str, Any], result: Any) -> None:
                    self._check_timeout(started, budget)
                    self._capture_sources(tool_name, arguments, result.output, observed_sources)

                try:
                    loop_result = ToolLoopExecutor(self.tool_router).run(
                        invoke_model=self.llm.invoke_json,
                        trace_name=f'subagent.{request.competitor}.{request.field_name}',
                        system_prompt=role.system_prompt,
                        user_payload=self._task_payload(request),
                        metadata={**metadata, 'node_name': 'collect', 'agent_name': 'CollectorDeepDiveSubagent'},
                        tool_names=list(role.allowed_tools),
                        max_tool_rounds=budget.max_rounds,
                        max_tool_calls=budget.max_tool_calls,
                        token_tracker=token_tracker,
                        fallback_to_plain_json=True,
                        after_tool=_after_tool,
                    )
                except ToolLoopError as exc:
                    history = exc.history
                    usage.rounds = exc.rounds
                    usage.tool_calls = exc.tool_calls
                    raise SubagentBudgetExceeded(str(exc)) from exc
                final_output = loop_result.final_output
                history = loop_result.history
                usage.rounds = loop_result.rounds
                usage.tool_calls = loop_result.tool_calls
                finish_subagent_trace(
                    subagent_span,
                    {
                        'final_output': final_output,
                        'budget_used': {
                            'rounds': usage.rounds,
                            'tool_calls': usage.tool_calls,
                            'prompt_tokens': token_tracker.prompt_tokens,
                            'completion_tokens': token_tracker.completion_tokens,
                            'total_tokens': token_tracker.total_tokens,
                        },
                    },
                )
        except SubagentBudgetExceeded as exc:
            status = 'budget_exhausted'
            error = str(exc)
        except Exception as exc:  # noqa: BLE001
            status = 'failed'
            error = str(exc)

        usage.prompt_tokens = token_tracker.prompt_tokens
        usage.completion_tokens = token_tracker.completion_tokens
        usage.total_tokens = token_tracker.total_tokens
        usage.latency_ms = int((time.monotonic() - started) * 1000)
        result = SubagentResult(
            subagent_id=request.subagent_id,
            status=status,
            competitor=request.competitor,
            field_name=request.field_name,
            usage=usage,
            new_evidences=self._build_evidences(request, final_output, observed_sources),
            verification_claims=self._string_list(final_output.get('verification_claims')),
            verification_conflicts=self._string_list(final_output.get('verification_conflicts')),
            verification_gaps=self._string_list(final_output.get('verification_gaps')),
            tool_history=history,
            error=error,
        )
        if status != 'completed' and error and not result.verification_gaps:
            result.verification_gaps.append(error)
        self.store.save_subagent_run(request=request, budget=budget, status=status, result=result)
        self._save_event(request, f'subagent.{status}', result.to_dict())
        return result

    @staticmethod
    def _protocol_prompt(role: SubagentRole) -> str:
        return (
            f'{role.system_prompt}\n'
            '只返回符合以下结构的严格 JSON：'
            '{"tool_calls":[{"name":"web.search","arguments":{}}],"final_output":null} '
            '或 {"tool_calls":[],"final_output":{"sources":[{"url":"..."}],'
            '"verification_claims":[],"verification_conflicts":[],"verification_gaps":[]}}.'
        )

    @staticmethod
    def _task_payload(request: SubagentRequest) -> dict[str, Any]:
        return {
            'parent_run_id': request.parent_run_id,
            'subagent_id': request.subagent_id,
            'industry': request.industry,
            'competitor': request.competitor,
            'field_name': request.field_name,
            'objective': request.objective,
            'seed_queries': request.seed_queries,
            'existing_evidences': request.existing_evidences,
        }

    @staticmethod
    def _metadata(request: SubagentRequest, budget: SubagentBudget) -> dict[str, Any]:
        return {
            'parent_run_id': request.parent_run_id,
            'run_id': request.parent_run_id,
            'subagent_id': request.subagent_id,
            'attempt': request.attempt,
            'competitor': request.competitor,
            'field_name': request.field_name,
            'budget_limit': {
                'max_rounds': budget.max_rounds,
                'max_tool_calls': budget.max_tool_calls,
                'max_tokens': budget.max_tokens,
                'timeout_s': budget.timeout_s,
            },
        }

    def _save_event(self, request: SubagentRequest, event_type: str, payload: dict[str, Any]) -> None:
        if request.parent_run_id == 'preview':
            return
        self.store.append_stage_event(request.parent_run_id, StageName.collect, event_type, payload)

    @staticmethod
    def _check_timeout(started: float, budget: SubagentBudget) -> None:
        if time.monotonic() - started >= budget.timeout_s:
            raise SubagentBudgetExceeded('subagent timeout budget exhausted')

    @classmethod
    def _capture_sources(
        cls,
        tool_name: str,
        arguments: dict[str, Any],
        output: dict[str, Any],
        observed_sources: dict[str, dict[str, Any]],
    ) -> None:
        if tool_name == 'web.search':
            for item in output.get('hits', []) if isinstance(output.get('hits', []), list) else []:
                if not isinstance(item, dict):
                    continue
                url = str(item.get('url', '') or '').strip()
                if url:
                    observed_sources[url] = {**observed_sources.get(url, {}), **item}
        elif tool_name == 'web.fetch':
            url = str(arguments.get('url', '') or '').strip()
            if url and url in observed_sources:
                observed_sources[url]['content_excerpt'] = str(output.get('content', '') or '')[:1000]
        elif tool_name == 'web.extract':
            url = str(arguments.get('url', '') or '').strip()
            if url and url in observed_sources:
                observed_sources[url]['content_excerpt'] = str(output.get('sanitized', '') or '')[:1000]
                observed_sources[url]['extract_fields'] = output.get('extract_fields', {})

    @classmethod
    def _build_evidences(
        cls,
        request: SubagentRequest,
        final_output: dict[str, Any],
        observed_sources: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        selected_urls: list[str] = []
        raw_sources = final_output.get('sources', [])
        if isinstance(raw_sources, list):
            for item in raw_sources:
                url = str(item.get('url', '') if isinstance(item, dict) else item).strip()
                if url and url in observed_sources and url not in selected_urls:
                    selected_urls.append(url)
        output: list[dict[str, Any]] = []
        for url in selected_urls:
            source = observed_sources[url]
            output.append(
                {
                    'query': str(source.get('query', '') or ''),
                    'title': str(source.get('title', '') or ''),
                    'source_url': url,
                    'snippet': str(source.get('snippet', '') or source.get('content_excerpt', '') or '')[:500],
                    'source_provider': str(source.get('source_provider', '') or 'subagent'),
                    'source_type': cls._infer_source_type(url),
                    'retrieval_method': 'collector_deep_dive_subagent',
                    'retrieval_status': 'ok',
                    'extract_fields': source.get('extract_fields', {}),
                    'confidence': 0.7,
                    'recency_score': 0.5,
                    'license_or_tos_note': 'public web source, subagent verification recorded',
                    'raw_content_path': '',
                    'content_excerpt': str(source.get('content_excerpt', '') or source.get('snippet', '') or '')[:1000],
                    'schema_field': request.field_name,
                    'query_template': str(source.get('query', '') or ''),
                    'recommended_source_type': 'subagent_verified',
                }
            )
        return output

    @staticmethod
    def _infer_source_type(url: str) -> str:
        host = urlparse(url).netloc.casefold()
        if any(item in host for item in ('reddit.com', 'zhihu.com', 'news.ycombinator.com')):
            return 'community'
        if any(item in host for item in ('g2.com', 'capterra.com', 'trustpilot.com')):
            return 'review'
        return 'official'

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]
