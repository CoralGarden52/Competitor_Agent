from __future__ import annotations

from app.agents.manager_agent import ManagerAgent


def test_manager_infers_new_action_types_from_tools() -> None:
    assert ManagerAgent._infer_action_type_from_tool('action.collect_initial') == 'collect_initial'
    assert ManagerAgent._infer_action_type_from_tool('action.collect_gap') == 'collect_gap'
    assert ManagerAgent._infer_action_type_from_tool('action.reanalyze_targets') == 'reanalyze_targets'
    assert ManagerAgent._infer_action_type_from_tool('action.redraft_report') == 'redraft_report'
