from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from harness.tools.handlers import (
    CorpusSearchHandler,
    CoverageSummaryHandler,
    GapSummaryHandler,
    LLMInvokeJsonHandler,
    ReportStatusHandler,
    StateSnapshotHandler,
    WebExtractHandler,
    WebFetchHandler,
    WebSearchHandler,
    WorkflowActionHandler,
)
from harness.tools.providers import ProviderRegistry, build_fetch_provider_catalog, build_search_provider_catalog
from harness.tools.registry import ToolRegistry
from harness.tools.router import EventSink, ToolRouter
from harness.tools.specs import (
    ACTION_COLLECT_GAP_SPEC,
    ACTION_COLLECT_INITIAL_SPEC,
    ACTION_FINALIZE_RUN_SPEC,
    ACTION_NORMALIZE_EVIDENCE_SPEC,
    ACTION_PLAN_SCOPE_SPEC,
    ACTION_REDRAFT_REPORT_SPEC,
    ACTION_REANALYZE_TARGETS_SPEC,
    ACTION_RUN_QA_SPEC,
    CORPUS_SEARCH_SPEC,
    LLM_INVOKE_JSON_SPEC,
    STATE_GET_COVERAGE_SUMMARY_SPEC,
    STATE_GET_GAP_SUMMARY_SPEC,
    STATE_GET_REPORT_STATUS_SPEC,
    STATE_GET_RUN_SNAPSHOT_SPEC,
    WEB_EXTRACT_SPEC,
    WEB_FETCH_SPEC,
    WEB_SEARCH_SPEC,
)


@dataclass
class ToolRuntime:
    registry: ToolRegistry
    router: ToolRouter
    provider_registry: ProviderRegistry


def build_tool_runtime(config: Any, *, event_sink: EventSink | None = None, store: Any | None = None) -> ToolRuntime:
    providers = ProviderRegistry(
        search_catalog=build_search_provider_catalog(config),
        fetch_catalog=build_fetch_provider_catalog(config),
        search_order=config.collector_search_order_list,
        fetch_order=config.collector_fetch_order_list,
        strict_search_order=config.collector_search_order_strict,
    )
    registry = ToolRegistry()
    registry.register(spec=WEB_SEARCH_SPEC, handler=WebSearchHandler(providers))
    registry.register(spec=WEB_FETCH_SPEC, handler=WebFetchHandler(providers))
    registry.register(spec=WEB_EXTRACT_SPEC, handler=WebExtractHandler())
    if store is not None:
        registry.register(spec=CORPUS_SEARCH_SPEC, handler=CorpusSearchHandler(store))
    return ToolRuntime(registry=registry, router=ToolRouter(registry, event_sink=event_sink), provider_registry=providers)


def register_internal_llm_tool(runtime: ToolRuntime, call_impl: Callable[..., dict[str, Any]]) -> None:
    runtime.registry.register(spec=LLM_INVOKE_JSON_SPEC, handler=LLMInvokeJsonHandler(call_impl))


def register_workflow_tools(runtime: ToolRuntime, get_service: Callable[[], Any]) -> None:
    runtime.registry.register(spec=STATE_GET_RUN_SNAPSHOT_SPEC, handler=StateSnapshotHandler(get_service))
    runtime.registry.register(spec=STATE_GET_COVERAGE_SUMMARY_SPEC, handler=CoverageSummaryHandler(get_service))
    runtime.registry.register(spec=STATE_GET_GAP_SUMMARY_SPEC, handler=GapSummaryHandler(get_service))
    runtime.registry.register(spec=STATE_GET_REPORT_STATUS_SPEC, handler=ReportStatusHandler(get_service))
    runtime.registry.register(spec=ACTION_PLAN_SCOPE_SPEC, handler=WorkflowActionHandler(get_service, 'plan_scope'))
    runtime.registry.register(spec=ACTION_COLLECT_INITIAL_SPEC, handler=WorkflowActionHandler(get_service, 'collect_initial'))
    runtime.registry.register(spec=ACTION_COLLECT_GAP_SPEC, handler=WorkflowActionHandler(get_service, 'collect_gap'))
    runtime.registry.register(spec=ACTION_NORMALIZE_EVIDENCE_SPEC, handler=WorkflowActionHandler(get_service, 'normalize_evidence'))
    runtime.registry.register(spec=ACTION_REANALYZE_TARGETS_SPEC, handler=WorkflowActionHandler(get_service, 'reanalyze_targets'))
    runtime.registry.register(spec=ACTION_REDRAFT_REPORT_SPEC, handler=WorkflowActionHandler(get_service, 'redraft_report'))
    runtime.registry.register(spec=ACTION_RUN_QA_SPEC, handler=WorkflowActionHandler(get_service, 'run_qa'))
    runtime.registry.register(spec=ACTION_FINALIZE_RUN_SPEC, handler=WorkflowActionHandler(get_service, 'finalize_run'))
