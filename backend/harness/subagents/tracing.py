from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from app.core.tracing_factory import get_tracing_runtime

logger = logging.getLogger(__name__)


def finish_subagent_trace(span: Any, outputs: dict[str, Any]) -> None:
    if span is None or not hasattr(span, 'end'):
        return
    try:
        span.end(outputs=outputs)
    except Exception as exc:
        logger.warning('Subagent trace finalization failed: %s', exc)


@contextmanager
def subagent_trace(*, name: str, run_type: str, inputs: dict[str, Any], metadata: dict[str, Any]) -> Iterator[Any]:
    runtime = get_tracing_runtime()
    if not runtime.langsmith_enabled or runtime.client is None:
        yield None
        return
    try:
        from langsmith.run_helpers import trace
        trace_context = trace(
            name=name,
            run_type=run_type,
            inputs=inputs,
            metadata=metadata,
            project_name=runtime.project,
            client=runtime.client,
        )
    except Exception as exc:
        logger.warning('Subagent trace creation failed for %s: %s', name, exc)
        yield None
        return
    with trace_context as span:
        yield span
