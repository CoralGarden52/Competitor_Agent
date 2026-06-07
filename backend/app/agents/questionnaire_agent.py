from __future__ import annotations

import concurrent.futures
import re

from app.core.agent_llm import AgentLLMClient, LLMCallError
from app.core.models import QuestionnaireDesign, QuestionnaireSignalChunk, RunState
from app.core.prompts.agent_prompts import (
    QUESTIONNAIRE_MARKDOWN_SYSTEM_PROMPT,
    QUESTIONNAIRE_REVIEW_SYSTEM_PROMPT,
    QUESTIONNAIRE_SIGNAL_EXTRACT_SYSTEM_PROMPT,
)

QUESTIONNAIRE_MAX_CHUNK_CHARS = 2800
QUESTIONNAIRE_SIGNAL_MAX_WORKERS = 6
QUESTIONNAIRE_REVIEW_MAX_ROUNDS = 2
QUESTIONNAIRE_ALLOWED_TITLES = {
    '二、核心结论',
    '三、竞品对比总览',
    '四、核心能力与产品形态',
    '五、商业化与定价',
    '六、用户反馈与采用信号',
    '七、核心优劣势与风险',
}
QUESTIONNAIRE_BANNED_PHRASES = (
    '设计意图',
    '关联字段',
    'objective',
    '识别用户流失',
    '内部提示词',
    'schema',
    'field_refs',
)
QUESTIONNAIRE_DEFAULT_TITLE = '竞品分析用户调研问卷'


class QuestionnaireAgent:
    def __init__(self, llm: AgentLLMClient):
        self.llm = llm

    def run_llm(
        self,
        state: RunState,
        *,
        target_audience: str = '竞品相关潜在用户或现有用户',
        objective: str = '验证竞品差异点、用户感知与转化障碍',
    ) -> QuestionnaireDesign:
        report = state.report
        report_markdown = report.markdown if report else ''
        chunks = self._split_report_for_questionnaire(report_markdown)
        if not chunks:
            raise LLMCallError(
                reason='empty_report',
                message='Questionnaire generation requires a non-empty report markdown.',
                attempt_count=1,
                retry_count_used=0,
            )
        chunks = self._filter_questionnaire_chunks(chunks)
        if not chunks:
            raise LLMCallError(
                reason='empty_questionnaire_chunks',
                message='No eligible questionnaire chunks remained after filtering report sections.',
                attempt_count=1,
                retry_count_used=0,
            )
        signals = self._extract_signals_parallel(
            chunks=chunks,
            state=state,
            target_audience=target_audience,
            objective=objective,
        )
        if not any(item.candidate_questions or item.candidate_dimensions or item.key_points for item in signals):
            raise LLMCallError(
                reason='empty_questionnaire_signals',
                message='Questionnaire signal extraction returned no usable content.',
                attempt_count=1,
                retry_count_used=0,
            )
        payload = {
            'target_audience': target_audience,
            'objective': objective,
            'questionnaire_signals': [item.model_dump(mode='json') for item in signals],
            'questionnaire_requirements': {
                'sections': 4,
                'question_count_range': '12-18',
                'forbidden_phrases': list(QUESTIONNAIRE_BANNED_PHRASES),
                'must_hide_internal_notes': True,
                'must_be_user_facing': True,
            },
        }
        markdown = ''
        for review_round in range(1, QUESTIONNAIRE_REVIEW_MAX_ROUNDS + 1):
            round_payload = dict(payload)
            if review_round > 1 and markdown:
                round_payload['revision_feedback'] = self._review_markdown(
                    state=state,
                    markdown=markdown,
                    target_audience=target_audience,
                    objective=objective,
                ).get('revision_feedback', '')
            markdown = self.llm.invoke_text(
                trace_name='agent.questionnaire.markdown',
                system_prompt=QUESTIONNAIRE_MARKDOWN_SYSTEM_PROMPT,
                user_payload=round_payload,
                metadata={
                    'run_id': state.run_id,
                    'node_name': 'questionnaire',
                    'agent_name': 'QuestionnaireAgent',
                    'model': self.llm.config.openai_model,
                    'industry': state.industry,
                    'competitor_count': len(state.effective_analysis_subject_names()),
                    'attempt': state.attempt,
                    'review_round': review_round,
                },
            ).strip()
            if not markdown:
                raise LLMCallError(
                    reason='empty_questionnaire_markdown',
                    message='Questionnaire markdown generation returned empty content.',
                    attempt_count=1,
                    retry_count_used=0,
                )
            self._assert_markdown_quality(markdown)
            review = self._review_markdown(
                state=state,
                markdown=markdown,
                target_audience=target_audience,
                objective=objective,
            )
            if bool(review.get('passed', False)):
                break
        return QuestionnaireDesign(
            title=self._extract_title(markdown),
            target_audience=target_audience,
            objective=objective,
            introduction='',
            estimated_minutes=8,
            sections=[],
            closing_message='',
            markdown=markdown,
        )

    def _review_markdown(
        self,
        *,
        state: RunState,
        markdown: str,
        target_audience: str,
        objective: str,
    ) -> dict:
        return self.llm.invoke_json(
            trace_name='agent.questionnaire.review',
            system_prompt=QUESTIONNAIRE_REVIEW_SYSTEM_PROMPT,
            user_payload={
                'target_audience': target_audience,
                'objective': objective,
                'questionnaire_markdown': markdown,
                'review_requirements': {
                    'sections': 4,
                    'question_count_range': '12-18',
                    'forbidden_phrases': list(QUESTIONNAIRE_BANNED_PHRASES),
                    'must_hide_internal_notes': True,
                    'must_be_user_facing': True,
                },
            },
            metadata={
                'run_id': state.run_id,
                'node_name': 'questionnaire',
                'agent_name': 'QuestionnaireReviewer',
                'model': self.llm.config.openai_model,
                'industry': state.industry,
                'competitor_count': len(state.effective_analysis_subject_names()),
                'attempt': state.attempt,
            },
        )

    def _split_report_for_questionnaire(self, markdown: str) -> list[dict[str, str]]:
        text = str(markdown or '').strip()
        if not text:
            return []
        lines = text.splitlines()
        chunks: list[dict[str, str]] = []
        current_title = 'report_overview'
        current_lines: list[str] = []

        def flush() -> None:
            nonlocal current_lines, current_title
            content = '\n'.join(current_lines).strip()
            if not content:
                return
            normalized_title = current_title.strip().lower()
            if normalized_title in {'参考来源', '参考来源_cont'} or '参考来源' in current_title:
                current_lines = []
                return
            chunks.append(
                {
                    'chunk_id': f'chunk_{len(chunks) + 1}',
                    'chunk_title': current_title,
                    'content': content[:QUESTIONNAIRE_MAX_CHUNK_CHARS],
                }
            )
            current_lines = []

        for line in lines:
            if line.startswith('## '):
                flush()
                current_title = line[3:].strip() or f'chunk_{len(chunks) + 1}'
                current_lines = [line]
                continue
            current_lines.append(line)
            joined = '\n'.join(current_lines)
            if len(joined) >= QUESTIONNAIRE_MAX_CHUNK_CHARS:
                flush()
                current_title = f'{current_title}_cont'

        flush()
        return chunks

    def _filter_questionnaire_chunks(self, chunks: list[dict[str, str]]) -> list[dict[str, str]]:
        filtered: list[dict[str, str]] = []
        for chunk in chunks:
            title = str(chunk.get('chunk_title', '')).strip()
            if title in QUESTIONNAIRE_ALLOWED_TITLES:
                filtered.append(chunk)
        return filtered

    def _extract_signals_parallel(
        self,
        *,
        chunks: list[dict[str, str]],
        state: RunState,
        target_audience: str,
        objective: str,
    ) -> list[QuestionnaireSignalChunk]:
        if len(chunks) == 1:
            return [self._extract_signals_from_chunk(chunks[0], state=state, target_audience=target_audience, objective=objective)]

        results: list[QuestionnaireSignalChunk] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(QUESTIONNAIRE_SIGNAL_MAX_WORKERS, len(chunks))) as executor:
            future_map = {
                executor.submit(
                    self._extract_signals_from_chunk,
                    chunk,
                    state=state,
                    target_audience=target_audience,
                    objective=objective,
                ): chunk
                for chunk in chunks
            }
            ordered: dict[str, QuestionnaireSignalChunk] = {}
            for future in concurrent.futures.as_completed(future_map):
                chunk = future_map[future]
                ordered[chunk['chunk_id']] = future.result()
        for chunk in chunks:
            signal = ordered.get(chunk['chunk_id'])
            if signal is not None:
                results.append(signal)
        return results

    def _extract_signals_from_chunk(
        self,
        chunk: dict[str, str],
        *,
        state: RunState,
        target_audience: str,
        objective: str,
    ) -> QuestionnaireSignalChunk:
        payload = {
            'target_audience': target_audience,
            'objective': objective,
            'chunk_id': chunk['chunk_id'],
            'chunk_title': chunk['chunk_title'],
            'report_chunk_markdown': chunk['content'],
        }
        result = self.llm.invoke_json(
            trace_name=f"agent.questionnaire.signals.{chunk['chunk_id']}",
            system_prompt=QUESTIONNAIRE_SIGNAL_EXTRACT_SYSTEM_PROMPT,
            user_payload=payload,
            metadata={
                'run_id': state.run_id,
                'node_name': 'questionnaire',
                'agent_name': 'QuestionnaireAgent',
                'model': self.llm.config.openai_model,
                'industry': state.industry,
                'competitor_count': len(state.effective_analysis_subject_names()),
                'attempt': state.attempt,
                'chunk_id': chunk['chunk_id'],
            },
        )
        return QuestionnaireSignalChunk.model_validate(result)

    @staticmethod
    def _markdown_from_design(design: QuestionnaireDesign) -> str:
        lines = [
            f'# {design.title}',
            '',
            f'目标用户：{design.target_audience}',
            f'调研目标：{design.objective}',
            f'预计时长：{design.estimated_minutes} 分钟',
            '',
            design.introduction,
        ]
        for section in design.sections:
            lines.extend(['', f'## {section.title}'])
            for index, question in enumerate(section.questions, start=1):
                lines.extend(['', f'{index}. {question.title}'])
                if question.options:
                    lines.append('   - 选项：')
                    for option in question.options:
                        lines.append(f'     - {option}')
                if question.question_type == 'scale' and question.scale_min is not None and question.scale_max is not None:
                    lines.append(f'   - 请按 {question.scale_min} 到 {question.scale_max} 分进行评价')
        if design.closing_message:
            lines.extend(['', '---', '', design.closing_message])
        return '\n'.join(lines).strip()

    def _assert_markdown_quality(self, markdown: str) -> None:
        issues: list[str] = []
        visible_text = str(markdown or '')
        for phrase in QUESTIONNAIRE_BANNED_PHRASES:
            if phrase.lower() in visible_text.lower():
                issues.append(f'问卷包含不应暴露给用户的内部术语：{phrase}')
        raw_schema_tokens = re.findall(r'\b[a-z]+(?:_[a-z0-9]+)+\b', visible_text, flags=re.IGNORECASE)
        if raw_schema_tokens:
            issues.append(f'问卷正文出现未翻译字段名：{", ".join(sorted(set(raw_schema_tokens))[:6])}')
        if '设计意图' in visible_text or '关联字段' in visible_text:
            issues.append('markdown 仍包含内部说明字段')
        if issues:
            raise ValueError('；'.join(issues))

    @staticmethod
    def _extract_title(markdown: str) -> str:
        for line in str(markdown or '').splitlines():
            text = line.strip()
            if not text:
                continue
            if text.startswith('#'):
                return text.lstrip('#').strip() or QUESTIONNAIRE_DEFAULT_TITLE
            return text[:80]
        return QUESTIONNAIRE_DEFAULT_TITLE
