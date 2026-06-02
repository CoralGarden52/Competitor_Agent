from __future__ import annotations

from app.core.workflow import CompetitorWorkflowService


def test_extract_tool_events_filters_tool_event_payload() -> None:
    events = [
        {'event_type': 'provider_event', 'payload': {'x': 1}},
        {'event_type': 'tool_event', 'payload': {'event_type': 'tool.called', 'tool_name': 'web.search'}},
        {'event_type': 'tool_event', 'payload': {'event_type': 'tool.succeeded', 'tool_name': 'web.fetch'}},
    ]
    result = CompetitorWorkflowService._extract_tool_events(events)
    assert len(result) == 2
    assert result[0]['tool_name'] == 'web.search'
    assert result[1]['tool_name'] == 'web.fetch'
