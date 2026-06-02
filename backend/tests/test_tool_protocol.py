from __future__ import annotations

from harness.tools.protocol import parse_tool_call_turn


def test_parse_tool_call_turn() -> None:
    payload = {
        'tool_calls': [{'name': 'web.search', 'arguments': {'q': 'x'}}],
        'final_output': None,
    }
    turn = parse_tool_call_turn(payload)
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == 'web.search'
    assert turn.tool_calls[0].arguments['q'] == 'x'
    assert turn.final_output is None
