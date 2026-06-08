from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SubagentRole:
    name: str
    allowed_tools: tuple[str, ...]
    system_prompt: str


def collector_deep_dive_role() -> SubagentRole:
    return SubagentRole(
        name='collector.deep_dive',
        allowed_tools=('web.search', 'web.fetch', 'web.extract'),
        system_prompt=(
            '你是一个在隔离上下文中运行的证据研究子代理。每次只调查一个竞品 schema 字段。'
            '需要时使用工具。只返回包含 tool_calls 和 final_output 的严格 JSON。'
            '完成调查后，final_output 必须包含 sources、verification_claims、verification_conflicts '
            '和 verification_gaps。每个来源 URL 都必须来自工具结果。'
            '优先使用相互独立的来源站点，并明确说明不确定性。'
        ),
    )
