from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.agent_llm import LLMCallError
from app.core.models import ChatTurnRequest, ChatTurnResponse, ChatTurnResult, EventRecord, RunState, StageName
from app.core.run_logging import log_run_output
from harness.tools import ToolRequest


@dataclass(frozen=True)
class ReportChunk:
    chunk_id: str
    heading_path: str
    text: str


class ReportContextCompactor:
    def __init__(self, *, short_window_limit: int = 8):
        self.short_window_limit = short_window_limit

    def compact(
        self,
        *,
        messages: list[dict[str, Any]],
        existing_memory: dict[str, Any] | None = None,
        next_work_memory: str = '',
    ) -> dict[str, Any]:
        return self._fallback_compact(
            messages=messages,
            existing_memory=existing_memory,
            next_work_memory=next_work_memory,
        )

    def split_layers(
        self,
        *,
        messages: list[dict[str, Any]],
        existing_memory: dict[str, Any] | None = None,
        next_work_memory: str = '',
    ) -> dict[str, Any]:
        existing = existing_memory or {}
        short_window = messages[-self.short_window_limit :]
        older = messages[: max(0, len(messages) - self.short_window_limit)]
        long_archive_refs = self._long_archive_refs(existing_memory=existing, older_messages=older)
        return {
            'short_window': short_window,
            'summary_source_messages': older[-24:],
            'long_archive_refs': long_archive_refs,
            'previous_mid_summary': str(existing.get('mid_summary', '') or '').strip(),
            'next_work_memory': (next_work_memory or str(existing.get('next_work_memory', '') or '')).strip(),
        }

    def _fallback_compact(
        self,
        *,
        messages: list[dict[str, Any]],
        existing_memory: dict[str, Any] | None = None,
        next_work_memory: str = '',
    ) -> dict[str, Any]:
        existing = existing_memory or {}
        layers = self.split_layers(messages=messages, existing_memory=existing_memory, next_work_memory=next_work_memory)
        previous_summary = str(existing.get('mid_summary', '') or '').strip()
        summary_lines: list[str] = []
        if previous_summary:
            summary_lines.append(previous_summary)
        for item in layers['summary_source_messages']:
            role = str(item.get('role', '') or 'message')
            content = re.sub(r'\s+', ' ', str(item.get('content', '') or '')).strip()
            if content:
                summary_lines.append(f'{role}: {content[:180]}')
        mid_summary = '\n'.join(summary_lines)[-4000:]

        return {
            'short_window': layers['short_window'],
            'mid_summary': mid_summary,
            'long_archive_refs': layers['long_archive_refs'],
            'next_work_memory': layers['next_work_memory'],
        }

    def _long_archive_refs(self, *, existing_memory: dict[str, Any], older_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        archived_ids = {str(item.get('message_id', '')) for item in existing_memory.get('long_archive_refs', []) if isinstance(item, dict)}
        long_archive_refs = [item for item in existing_memory.get('long_archive_refs', []) if isinstance(item, dict)]
        for item in older_messages[:-24]:
            message_id = str(item.get('message_id', '') or '')
            if message_id and message_id not in archived_ids:
                long_archive_refs.append(
                    {
                        'message_id': message_id,
                        'turn_id': str(item.get('turn_id', '') or ''),
                        'role': str(item.get('role', '') or ''),
                    }
                )
                archived_ids.add(message_id)
        return long_archive_refs[-200:]


class ReportMemoryCompactionAgent:
    def __init__(self, *, llm: Any, short_window_limit: int = 8, fallback_compactor: ReportContextCompactor | None = None):
        self.llm = llm
        self.compactor = fallback_compactor or ReportContextCompactor(short_window_limit=short_window_limit)

    def compact(
        self,
        *,
        run_id: str,
        conversation_id: str,
        messages: list[dict[str, Any]],
        existing_memory: dict[str, Any] | None = None,
        next_work_memory: str = '',
    ) -> dict[str, Any]:
        layers = self.compactor.split_layers(
            messages=messages,
            existing_memory=existing_memory,
            next_work_memory=next_work_memory,
        )
        if not layers['summary_source_messages'] and not layers['previous_mid_summary']:
            return {
                'short_window': layers['short_window'],
                'mid_summary': '',
                'long_archive_refs': layers['long_archive_refs'],
                'next_work_memory': layers['next_work_memory'],
                '_compaction_fallback': False,
                '_compaction_error': '',
            }
        try:
            result = self.llm.invoke_json(
                trace_name='report_conversation_memory_compact',
                system_prompt=self._system_prompt(),
                user_payload={
                    'run_id': run_id,
                    'conversation_id': conversation_id,
                    'previous_mid_summary': layers['previous_mid_summary'],
                    'messages_to_summarize': [self._message_for_summary(item) for item in layers['summary_source_messages']],
                    'candidate_next_work_memory': layers['next_work_memory'],
                    'summary_policy': {
                        'target_chars': '2000-4000',
                        'short_window_message_count': len(layers['short_window']),
                        'long_archive_ref_count': len(layers['long_archive_refs']),
                    },
                },
                metadata={'run_id': run_id, 'node_name': 'chat', 'agent_name': 'ReportMemoryCompactionAgent'},
            )
            mid_summary = str(result.get('mid_summary', '') or '').strip()[-4000:]
            if not mid_summary and layers['summary_source_messages']:
                raise LLMCallError(reason='empty_summary', message='memory compaction returned empty mid_summary')
            return {
                'short_window': layers['short_window'],
                'mid_summary': mid_summary,
                'long_archive_refs': layers['long_archive_refs'],
                'next_work_memory': (
                    str(result.get('next_work_memory', '') or '').strip()
                    or layers['next_work_memory']
                ),
                '_compaction_fallback': False,
                '_compaction_error': '',
            }
        except Exception as exc:
            memory = self.compactor._fallback_compact(
                messages=messages,
                existing_memory=existing_memory,
                next_work_memory=next_work_memory,
            )
            memory['_compaction_fallback'] = True
            memory['_compaction_error'] = str(exc)[:500]
            return memory

    def _system_prompt(self) -> str:
        return (
            '你是竞品分析报告对话的 Memory Compaction Agent。'
            '请把旧的中窗摘要和本轮进入中窗的历史消息融合成滚动摘要。'
            '只保留对后续对话有用的信息：用户目标、偏好、已确认事实、已执行动作、报告修改状态、证据限制、未完成事项。'
            '不要编造没有出现在输入里的事实，不要覆盖仍未完成的下一步工作记忆。'
            '不要逐字复制短窗原文或大段消息；需要去重、归并并压缩到约 2000-4000 字符。'
            '返回 JSON 字段：mid_summary, next_work_memory。'
        )

    def _message_for_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            'message_id': str(item.get('message_id', '') or ''),
            'turn_id': str(item.get('turn_id', '') or ''),
            'role': str(item.get('role', '') or 'message'),
            'content': re.sub(r'\s+', ' ', str(item.get('content', '') or '')).strip()[:4000],
            'metadata': item.get('metadata', {}) if isinstance(item.get('metadata', {}), dict) else {},
            'created_at': str(item.get('created_at', '') or ''),
        }


def split_report_chunks(markdown: str, *, max_chars: int = 2200) -> list[ReportChunk]:
    text = str(markdown or '').strip()
    if not text:
        return []

    headings: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_heading = '报告'
    current_lines: list[str] = []
    for line in text.splitlines():
        match = re.match(r'^(#{1,6})\s+(.+?)\s*$', line)
        if match:
            if current_lines:
                sections.append((current_heading, current_lines))
            level = len(match.group(1))
            title = match.group(2).strip()
            headings = headings[: level - 1]
            headings.append(title)
            current_heading = ' > '.join(headings)
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_heading, current_lines))

    chunks: list[ReportChunk] = []
    for section_index, (heading, lines) in enumerate(sections):
        section_text = '\n'.join(lines).strip()
        if len(section_text) <= max_chars:
            chunks.append(ReportChunk(chunk_id=f'chunk_{section_index}', heading_path=heading, text=section_text))
            continue
        start = 0
        part = 0
        while start < len(section_text):
            end = min(len(section_text), start + max_chars)
            chunks.append(
                ReportChunk(
                    chunk_id=f'chunk_{section_index}_{part}',
                    heading_path=f'{heading} / part {part + 1}',
                    text=section_text[start:end],
                )
            )
            start = end
            part += 1
    return chunks


def select_report_chunks(chunks: list[ReportChunk], query: str, *, limit: int = 5) -> list[ReportChunk]:
    if not chunks:
        return []
    tokens: set[str] = set()
    for token in re.findall(r'[A-Za-z0-9_]+|[\u4e00-\u9fff]+', query or ''):
        if len(token) < 2:
            continue
        lowered = token.lower()
        tokens.add(lowered)
        if re.fullmatch(r'[\u4e00-\u9fff]+', token) and len(token) > 2:
            tokens.update(token[index : index + 2] for index in range(0, len(token) - 1))
    scored: list[tuple[int, ReportChunk]] = []
    for chunk in chunks:
        haystack = f'{chunk.heading_path}\n{chunk.text}'.lower()
        score = sum(1 for token in tokens if token in haystack)
        if score:
            scored.append((score, chunk))
    if not scored:
        return chunks[:limit]
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[:limit]]


class ReportConversationService:
    def __init__(self, workflow_service: Any):
        self.workflow = workflow_service
        self.store = workflow_service.store
        self.compactor = ReportMemoryCompactionAgent(llm=workflow_service.agent_llm)

    def start_turn(self, run_id: str, request: ChatTurnRequest) -> ChatTurnResponse | None:
        state = self.store.get_state(run_id)
        if state is None:
            return None
        conversation = self.store.get_or_create_conversation(run_id)
        conversation_id = str(conversation['conversation_id'])
        turn = self.store.create_conversation_turn(
            run_id=run_id,
            conversation_id=conversation_id,
            mode=request.mode,
            allow_web_collect=request.allow_web_collect,
            auto_apply=request.auto_apply,
            user_message=request.message,
            status='running',
        )
        turn_id = str(turn['turn_id'])
        self.store.append_conversation_message(
            run_id=run_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            role='user',
            content=request.message,
            metadata={'mode': request.mode, 'allow_web_collect': request.allow_web_collect, 'auto_apply': request.auto_apply},
        )
        self._emit_turn_stream(
            turn_id,
            'chat_bootstrap',
            {'run_id': run_id, 'conversation_id': conversation_id, 'turn_id': turn_id, 'status': 'running'},
        )
        self._emit_event(state, 'chat_turn_started', {'conversation_id': conversation_id, 'turn_id': turn_id})
        self.workflow._run_executor.submit(self._execute_turn, run_id, conversation_id, turn_id, request)
        return ChatTurnResponse(
            run_id=run_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            status='running',
            message='chat turn queued',
        )

    def conversation_payload(self, run_id: str) -> dict[str, Any]:
        cache = getattr(self.workflow, 'cache', None)
        if cache is not None:
            cached = cache.get_chat_payload(run_id)
            if isinstance(cached, dict):
                return cached
        state = self.store.get_state(run_id)
        if state is None:
            return {'run_id': run_id, 'status': 'not_found'}
        conversation = self.store.get_or_create_conversation(run_id)
        conversation_id = str(conversation['conversation_id'])
        payload = {
            'run_id': run_id,
            'conversation': conversation,
            'messages': self.store.list_conversation_messages(run_id=run_id, conversation_id=conversation_id),
            'turns': self.store.list_conversation_turns(run_id=run_id, conversation_id=conversation_id),
            'memory': self.store.get_conversation_memory(conversation_id),
            'report_revisions': self.store.list_report_revisions(run_id=run_id, conversation_id=conversation_id),
        }
        if cache is not None:
            cache.set_chat_payload(run_id, payload)
        return payload

    def turn_payload(self, run_id: str, turn_id: str) -> ChatTurnResult | None:
        turn = self.store.get_conversation_turn(turn_id)
        if turn is None or str(turn.get('run_id', '')) != run_id:
            return None
        result = turn.get('result', {}) if isinstance(turn.get('result', {}), dict) else {}
        return ChatTurnResult(
            run_id=run_id,
            conversation_id=str(turn.get('conversation_id', '')),
            turn_id=turn_id,
            status=str(turn.get('status', '')),
            assistant_answer=str(result.get('assistant_answer', '') or ''),
            actions_taken=[str(item) for item in result.get('actions_taken', []) if str(item).strip()],
            report_updated=bool(result.get('report_updated', False)),
            report_revision_id=str(result.get('report_revision_id', '') or ''),
            source_refs=[str(item) for item in result.get('source_refs', []) if str(item).strip()],
            memory_snapshot=result.get('memory_snapshot', {}) if isinstance(result.get('memory_snapshot', {}), dict) else {},
            error_message=str(turn.get('error_message', '') or result.get('error_message', '') or ''),
        )

    def _execute_turn(self, run_id: str, conversation_id: str, turn_id: str, request: ChatTurnRequest) -> None:
        try:
            state = self.store.get_state(run_id)
            if state is None:
                raise ValueError('run not found')
            result = self._build_turn_result(state, conversation_id, turn_id, request)
            self.store.append_conversation_message(
                run_id=run_id,
                conversation_id=conversation_id,
                turn_id=turn_id,
                role='assistant',
                content=str(result.get('assistant_answer', '') or ''),
                metadata={'actions_taken': result.get('actions_taken', []), 'source_refs': result.get('source_refs', [])},
            )
            messages = self.store.list_conversation_messages(run_id=run_id, conversation_id=conversation_id)
            existing_memory = self.store.get_conversation_memory(conversation_id)
            self._emit_event(
                state,
                'chat.memory.compaction.started',
                {
                    'conversation_id': conversation_id,
                    'turn_id': turn_id,
                    'message_count': len(messages),
                    'short_window_count': len(existing_memory.get('short_window', []) if isinstance(existing_memory.get('short_window', []), list) else []),
                    'has_mid_summary': bool(str(existing_memory.get('mid_summary', '') or '').strip()),
                    'long_archive_ref_count': len(existing_memory.get('long_archive_refs', []) if isinstance(existing_memory.get('long_archive_refs', []), list) else []),
                },
            )
            memory = self.compactor.compact(
                run_id=run_id,
                conversation_id=conversation_id,
                messages=messages,
                existing_memory=existing_memory,
                next_work_memory=str(result.get('next_work_memory', '') or ''),
            )
            compaction_fallback = bool(memory.pop('_compaction_fallback', False))
            compaction_error = str(memory.pop('_compaction_error', '') or '')
            if compaction_fallback:
                self._emit_event(
                    state,
                    'chat.memory.compaction.failed',
                    {
                        'conversation_id': conversation_id,
                        'turn_id': turn_id,
                        'short_window_count': len(memory.get('short_window', []) if isinstance(memory.get('short_window', []), list) else []),
                        'has_mid_summary': bool(str(memory.get('mid_summary', '') or '').strip()),
                        'long_archive_ref_count': len(memory.get('long_archive_refs', []) if isinstance(memory.get('long_archive_refs', []), list) else []),
                        'error': compaction_error,
                    },
                )
            self._emit_event(
                state,
                'chat.memory.compaction.completed',
                {
                    'conversation_id': conversation_id,
                    'turn_id': turn_id,
                    'short_window_count': len(memory.get('short_window', []) if isinstance(memory.get('short_window', []), list) else []),
                    'has_mid_summary': bool(str(memory.get('mid_summary', '') or '').strip()),
                    'long_archive_ref_count': len(memory.get('long_archive_refs', []) if isinstance(memory.get('long_archive_refs', []), list) else []),
                    'has_next_work_memory': bool(str(memory.get('next_work_memory', '') or '').strip()),
                },
            )
            self.store.save_conversation_memory(
                conversation_id=conversation_id,
                run_id=run_id,
                short_window=memory['short_window'],
                mid_summary=memory['mid_summary'],
                long_archive_refs=memory['long_archive_refs'],
                next_work_memory=memory['next_work_memory'],
            )
            result['memory_snapshot'] = memory
            result.pop('next_work_memory', None)
            self.store.update_conversation_turn(turn_id=turn_id, status='completed', result=result)
            self._emit_turn_stream(
                turn_id,
                'chat_done',
                {'run_id': run_id, 'conversation_id': conversation_id, 'turn_id': turn_id, 'result': result},
            )
            self._emit_event(state, 'chat_turn_completed', {'conversation_id': conversation_id, 'turn_id': turn_id, 'result': result})
        except Exception as exc:
            self.store.update_conversation_turn(turn_id=turn_id, status='failed', result={'error_message': str(exc)}, error_message=str(exc))
            self._emit_turn_stream(
                turn_id,
                'chat_error',
                {'run_id': run_id, 'conversation_id': conversation_id, 'turn_id': turn_id, 'error': str(exc)},
            )
            state = self.store.get_state(run_id)
            if state is not None:
                self._emit_event(state, 'chat_turn_failed', {'conversation_id': conversation_id, 'turn_id': turn_id, 'error': str(exc)})
        finally:
            self.workflow.chat_stream_broker.close(turn_id)

    def _build_turn_result(
        self,
        state: RunState,
        conversation_id: str,
        turn_id: str,
        request: ChatTurnRequest,
    ) -> dict[str, Any]:
        markdown = state.report.markdown if state.report is not None else ''
        self._emit_event(state, 'chat.context.loading', {'conversation_id': conversation_id, 'turn_id': turn_id})
        self._emit_turn_stream(turn_id, 'chat_progress', {'stage': 'context_loading', 'message': '正在读取报告分片和对话 memory...'})
        chunks = self._get_report_chunks(run_id=state.run_id, markdown=markdown)
        selected_chunks = select_report_chunks(chunks, request.message)
        source_refs = [f'report:{chunk.chunk_id}:{chunk.heading_path}' for chunk in selected_chunks]
        self._emit_event(
            state,
            'chat.report_chunks.loaded',
            {
                'conversation_id': conversation_id,
                'turn_id': turn_id,
                'chunk_count': len(selected_chunks),
                'headings': [chunk.heading_path for chunk in selected_chunks[:5]],
            },
        )
        self._update_turn_progress(
            turn_id,
            assistant_answer=(
                f'已读取报告分片 {len(selected_chunks)} 个：'
                f'{", ".join(chunk.heading_path for chunk in selected_chunks[:3]) or "无匹配章节"}。正在检索相关语料。'
            ),
            actions_taken=['report.get_chunks'] if selected_chunks else [],
            source_refs=source_refs,
        )
        self._emit_turn_stream(
            turn_id,
            'chat_progress',
            {'stage': 'report_chunks_loaded', 'message': f'已读取报告分片 {len(selected_chunks)} 个，正在检索相关语料。'},
        )
        self._emit_event(
            state,
            'chat.corpus_search.started',
            {
                'conversation_id': conversation_id,
                'turn_id': turn_id,
                'query': request.message,
            },
        )
        corpus_refs = self._search_corpus_refs(state, request.message)
        if corpus_refs:
            source_refs.extend([str(item.get('source_url') or item.get('corpus_id') or '') for item in corpus_refs])
            self._emit_event(state, 'chat_tool_event', {'tool': 'corpus.search', 'count': len(corpus_refs), 'turn_id': turn_id})
        self._emit_event(
            state,
            'chat.corpus_search.completed',
            {
                'conversation_id': conversation_id,
                'turn_id': turn_id,
                'result_count': len(corpus_refs),
                'sources': [str(item.get('source_url') or item.get('title') or '') for item in corpus_refs[:5]],
            },
        )
        self._emit_turn_stream(
            turn_id,
            'chat_tool',
            {'tool': 'corpus.search', 'status': 'completed', 'count': len(corpus_refs)},
        )
        memory = self.store.get_conversation_memory(conversation_id)
        self._emit_event(
            state,
            'chat.memory.loaded',
            {
                'conversation_id': conversation_id,
                'turn_id': turn_id,
                'short_window_count': len(memory.get('short_window', []) if isinstance(memory.get('short_window', []), list) else []),
                'has_mid_summary': bool(str(memory.get('mid_summary', '') or '').strip()),
                'has_next_work_memory': bool(str(memory.get('next_work_memory', '') or '').strip()),
            },
        )
        collect_decision = self._decide_web_collect(
            state=state,
            request=request,
            selected_chunks=selected_chunks,
            corpus_refs=corpus_refs,
            memory=memory,
        )
        needs_web_collect = bool(collect_decision.get('needs_web_collect', False))
        web_queries = [str(item).strip() for item in collect_decision.get('queries', []) if str(item).strip()][:2]
        decision_reason = str(collect_decision.get('reason', '') or '').strip()
        decision_source = str(collect_decision.get('decision_source', '') or 'llm')
        fallback_needed = self._needs_web_collect(
            message=request.message,
            selected_chunks=selected_chunks,
            corpus_refs=corpus_refs,
            allow_web_collect=request.allow_web_collect,
        )
        web_refs: list[dict[str, Any]] = []
        if needs_web_collect:
            self._emit_event(
                state,
                'chat.collect.decision',
                {
                    'conversation_id': conversation_id,
                    'turn_id': turn_id,
                    'decision': 'executed',
                    'reason': decision_reason or 'manager_decided_external_context_is_needed',
                    'decision_source': decision_source,
                    'query': request.message,
                    'queries': web_queries,
                },
            )
            web_refs = self._collect_web_refs(state=state, conversation_id=conversation_id, turn_id=turn_id, message=request.message, queries=web_queries)
            source_refs.extend([str(item.get('source_url', '') or '') for item in web_refs if str(item.get('source_url', '') or '').strip()])
        else:
            self._emit_event(
                state,
                'chat.collect.decision',
                {
                    'conversation_id': conversation_id,
                    'turn_id': turn_id,
                    'decision': 'not_needed' if request.allow_web_collect else 'skipped',
                    'reason': decision_reason or ('report_or_corpus_context_available' if request.allow_web_collect else 'web_collect_disabled'),
                    'decision_source': decision_source,
                    'fallback_would_collect': fallback_needed,
                },
            )
        self._update_turn_progress(
            turn_id,
            assistant_answer=f'已检索相关语料 {len(corpus_refs)} 条。正在判断是否需要补写报告并生成回答。',
            actions_taken=self._progress_actions(selected_chunks=selected_chunks, corpus_refs=corpus_refs, web_refs=web_refs),
            source_refs=source_refs,
        )
        self._emit_turn_stream(
            turn_id,
            'chat_progress',
            {'stage': 'llm_preparing', 'message': '正在生成回答...'},
        )
        allow_report_update = self._allows_report_update(request)
        self._emit_event(state, 'chat.llm.started', {'conversation_id': conversation_id, 'turn_id': turn_id, 'allow_report_update': allow_report_update})
        llm_result = self._invoke_llm(
            state,
            request,
            selected_chunks,
            corpus_refs,
            web_refs,
            memory,
            allow_report_update=allow_report_update,
            turn_id=turn_id,
        )
        self._emit_event(
            state,
            'chat.llm.completed',
            {
                'conversation_id': conversation_id,
                'turn_id': turn_id,
                'intent': str(llm_result.get('intent', '') or ''),
                'report_updated_requested': bool(llm_result.get('report_updated', False)),
                'actions_taken': llm_result.get('actions_taken', []),
            },
        )
        should_update = bool(llm_result.get('report_updated', False)) and allow_report_update and request.auto_apply and state.report is not None
        revised_markdown = str(llm_result.get('revised_markdown', '') or '').strip()
        if should_update:
            self._emit_event(
                state,
                'chat.report_patch.planned',
                {
                    'conversation_id': conversation_id,
                    'turn_id': turn_id,
                    'candidate_length': len(revised_markdown),
                    'before_length': len(markdown),
                },
            )
            revised_markdown = self._safe_revised_markdown(
                before_markdown=markdown,
                candidate_markdown=revised_markdown,
                request_message=request.message,
                selected_chunks=selected_chunks,
                corpus_refs=corpus_refs,
            )
        revision_id = ''
        if should_update and revised_markdown and revised_markdown != markdown:
            revision_id = self._apply_report_revision(
                state=state,
                conversation_id=conversation_id,
                turn_id=turn_id,
                before_markdown=markdown,
                after_markdown=revised_markdown,
                patch_summary=str(llm_result.get('patch_summary', '') or '根据对话请求更新报告'),
                reason=request.message,
                source_refs=source_refs,
            )
        elif bool(llm_result.get('report_updated', False)) and (not request.auto_apply or not allow_report_update):
            self._emit_event(
                state,
                'chat.report_patch.skipped',
                {
                    'conversation_id': conversation_id,
                    'turn_id': turn_id,
                    'reason': 'auto_apply_disabled' if not request.auto_apply else 'report_update_not_explicitly_requested',
                },
            )

        report_updated = bool(revision_id)
        actions_taken = self._normalize_actions(llm_result.get('actions_taken', []))
        if not report_updated:
            actions_taken = [item for item in actions_taken if item != 'report.apply_patch']
        if selected_chunks and 'report.get_chunks' not in actions_taken:
            actions_taken.insert(0, 'report.get_chunks')
        if corpus_refs and 'corpus.search' not in actions_taken:
            actions_taken.append('corpus.search')
        if web_refs:
            for action_name in ('web.search', 'web.fetch', 'web.extract'):
                if action_name not in actions_taken:
                    actions_taken.append(action_name)
        if report_updated and 'report.apply_patch' not in actions_taken:
            actions_taken.append('report.apply_patch')
        answer = self._format_answer_response_v3(
            base_answer=str(llm_result.get('assistant_answer', '') or ''),
            actions_taken=actions_taken,
            report_updated=report_updated,
            patch_summary=str(llm_result.get('patch_summary', '') or ''),
            source_refs=[ref for ref in source_refs if ref],
            web_refs=web_refs,
        )

        return {
            'assistant_answer': answer,
            'actions_taken': actions_taken,
            'report_updated': report_updated,
            'report_revision_id': revision_id,
            'source_refs': [ref for ref in source_refs if ref],
            'next_work_memory': str(llm_result.get('next_work_memory', '') or ''),
        }

    def _invoke_llm(
        self,
        state: RunState,
        request: ChatTurnRequest,
        chunks: list[ReportChunk],
        corpus_refs: list[dict[str, Any]],
        web_refs: list[dict[str, Any]],
        memory: dict[str, Any],
        *,
        allow_report_update: bool,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            'run_id': state.run_id,
            'industry': state.industry,
            'user_message': request.message,
            'mode': request.mode,
            'allow_web_collect': request.allow_web_collect,
            'auto_apply': request.auto_apply,
            'allow_report_update': allow_report_update,
            'memory': {
                'short_window': memory.get('short_window', []),
                'mid_summary': memory.get('mid_summary', ''),
                'next_work_memory': memory.get('next_work_memory', ''),
            },
            'report_chunks': [{'chunk_id': item.chunk_id, 'heading_path': item.heading_path, 'text': item.text} for item in chunks],
            'corpus_refs': [
                {
                    'title': item.get('title', ''),
                    'source_url': item.get('source_url', ''),
                    'summary': item.get('summary', ''),
                }
                for item in corpus_refs
            ],
            'web_refs': [
                {
                    'title': item.get('title', ''),
                    'source_url': item.get('source_url', ''),
                    'snippet': item.get('snippet', ''),
                    'summary': item.get('summary', ''),
                    'content_excerpt': item.get('content_excerpt', ''),
                    'provider': item.get('provider', ''),
                }
                for item in web_refs
            ],
            'evidence_policy': {
                'report_is_context_not_the_only_source': True,
                'public_facts_must_come_from_corpus_or_web_refs': True,
                'state_evidence_gaps_when_refs_are_insufficient': True,
            },
            'current_report_markdown': state.report.markdown if state.report is not None and allow_report_update else '',
        }
        system_prompt = (
            'You are the answer agent for a multi-turn competitor-analysis chat. Answer the user directly in the same language as the user. '
            'Default to answer_only. For ordinary follow-up requests such as "supplement more advantages", provide the supplemental content in assistant_answer; do not update the report. '
            'Only set report_updated=true and return revised_markdown when allow_report_update is true and the user explicitly asks to write, modify, update, save, or apply content to the report. '
            'Do not respond with a plan such as "I will collect later" when web_refs are provided; synthesize the existing report context and collected refs into the actual answer. '
            'Use memory to understand prior turns, but do not expose internal next-step memory as the answer. '
            'actions_taken must be a JSON array containing only these tool ids when applicable: report.get_chunks, corpus.search, web.search, web.fetch, web.extract, report.apply_patch. '
            '你是竞品分析报告的多轮对话 Agent。每轮必须结合用户消息、conversation memory、报告片段、本地语料和 web_refs 回答。'
            '报告只是上下文之一，不要只复述报告；如果 web_refs 或 corpus_refs 提供了公开证据，应优先用于回答用户的公开事实问题。'
            '新增事实必须来自 corpus_refs 或 web_refs；证据不足时明确说明缺口，不要编造。'
            '如果用户要求修改报告，再决定是否返回 revised_markdown；否则优先直接回答问题。'
            '你是竞品分析报告的对话式 Manager Agent。根据用户消息、memory、报告分片和证据摘要，'
            '决定只回答还是修改报告。新增事实必须来自给定证据；证据不足时说明需要采集，不要编造。'
            '返回 JSON: intent, assistant_answer, report_updated, revised_markdown, patch_summary, '
            'actions_taken, source_refs, next_work_memory。'
        )
        try:
            result = self.workflow.agent_llm.invoke_json(
                trace_name='report_conversation_turn',
                system_prompt=system_prompt,
                user_payload=payload,
                metadata={'run_id': state.run_id, 'node_name': 'chat', 'agent_name': 'ReportConversationManager'},
            )
            if isinstance(result, dict) and str(result.get('assistant_answer', '')).strip():
                return result
        except LLMCallError:
            pass
        except Exception:
            pass
        return self._fallback_result(state, request, chunks, corpus_refs, web_refs)

    def _invoke_llm_streaming_answer(
        self,
        state: RunState,
        request: ChatTurnRequest,
        chunks: list[ReportChunk],
        corpus_refs: list[dict[str, Any]],
        web_refs: list[dict[str, Any]],
        memory: dict[str, Any],
        *,
        turn_id: str,
    ) -> dict[str, Any]:
        system_prompt = (
            'You are the answer agent for a multi-turn competitor-analysis chat. '
            'Answer the user directly in the same language as the user. '
            'Use the user message, conversation memory, selected report chunks, local corpus refs, and optional web refs. '
            'Do not mention internal workflow or say you will answer later. '
            'If evidence is insufficient, say what is missing briefly and avoid fabrication. '
            'Do not output JSON, markdown code fences, or tool metadata. Return answer text only.'
        )
        payload = {
            'run_id': state.run_id,
            'industry': state.industry,
            'user_message': request.message,
            'memory': {
                'short_window': memory.get('short_window', []),
                'mid_summary': memory.get('mid_summary', ''),
                'next_work_memory': memory.get('next_work_memory', ''),
            },
            'report_chunks': [{'chunk_id': item.chunk_id, 'heading_path': item.heading_path, 'text': item.text} for item in chunks],
            'corpus_refs': [
                {'title': item.get('title', ''), 'source_url': item.get('source_url', ''), 'summary': item.get('summary', '')}
                for item in corpus_refs
            ],
            'web_refs': [
                {
                    'title': item.get('title', ''),
                    'source_url': item.get('source_url', ''),
                    'summary': item.get('summary', ''),
                    'snippet': item.get('snippet', ''),
                }
                for item in web_refs
            ],
        }
        actions_taken = self._progress_actions(selected_chunks=chunks, corpus_refs=corpus_refs, web_refs=web_refs)
        if chunks and 'report.get_chunks' not in actions_taken:
            actions_taken.insert(0, 'report.get_chunks')
        source_refs = [f'report:{item.chunk_id}:{item.heading_path}' for item in chunks]
        source_refs.extend([str(item.get('source_url') or item.get('corpus_id') or '') for item in corpus_refs if str(item.get('source_url') or item.get('corpus_id') or '').strip()])
        source_refs.extend([str(item.get('source_url', '') or '') for item in web_refs if str(item.get('source_url', '') or '').strip()])
        answer_parts: list[str] = []
        try:
            for delta in self.workflow.agent_llm.invoke_text_stream(
                trace_name='report_conversation_turn_stream',
                system_prompt=system_prompt,
                user_payload=payload,
                metadata={'run_id': state.run_id, 'node_name': 'chat', 'agent_name': 'ReportConversationManager'},
                temperature=0.2,
            ):
                answer_parts.append(delta)
                self._emit_turn_stream(turn_id, 'chat_delta', {'delta': delta})
                if len(answer_parts) % 8 == 0:
                    self._update_turn_progress(
                        turn_id,
                        assistant_answer=''.join(answer_parts),
                        actions_taken=actions_taken,
                        source_refs=source_refs,
                    )
        except Exception:
            fallback = self._fallback_result(state, request, chunks, corpus_refs, web_refs)
            answer = str(fallback.get('assistant_answer', '') or '')
            for char in answer:
                self._emit_turn_stream(turn_id, 'chat_delta', {'delta': char})
            return fallback
        answer = ''.join(answer_parts).strip()
        if not answer:
            return self._fallback_result(state, request, chunks, corpus_refs, web_refs)
        return {
            'intent': 'answer_only',
            'assistant_answer': answer,
            'report_updated': False,
            'revised_markdown': '',
            'patch_summary': '',
            'actions_taken': actions_taken,
            'next_work_memory': '等待用户下一轮追问，必要时补充外部证据或转为 edit_report。',
        }

    def _fallback_result(
        self,
        state: RunState,
        request: ChatTurnRequest,
        chunks: list[ReportChunk],
        corpus_refs: list[dict[str, Any]],
        web_refs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        markdown = state.report.markdown if state.report is not None else ''
        wants_edit = request.mode == 'edit_report' or (
            request.mode == 'auto'
            and any(token in request.message for token in ('修改', '补充', '加入', '新增', '完善', '改进', '更新', 'edit', 'add', 'revise'))
        )
        wants_external = any(token in request.message for token in ('采集', '爬取', '网页', '最新', '外部', '证据', 'source', 'web'))
        wants_edit = wants_edit and self._allows_report_update(request)
        context_text = '\n\n'.join(chunk.text for chunk in chunks[:3]).strip()
        if wants_edit:
            if not markdown.strip():
                return {
                    'intent': 'report_edit',
                    'assistant_answer': '当前 run 还没有可修改的报告。请先生成报告，或让我基于现有分析重新起草。',
                    'report_updated': False,
                    'revised_markdown': '',
                    'patch_summary': '',
                    'actions_taken': ['report.get_chunks'],
                    'next_work_memory': '等待报告生成后再应用用户提出的修改点。',
                }
            addition = self._build_appendix(request.message, context_text, corpus_refs)
            revised = self._append_conversation_section(markdown, addition)
            return {
                'intent': 'report_edit',
                'assistant_answer': '已根据你的要求补充报告，并保留了本轮修改记录。新增内容基于当前报告上下文；需要外部事实时会标记为待采集。',
                'report_updated': True,
                'revised_markdown': revised,
                'patch_summary': '追加对话补充内容',
                'actions_taken': ['report.get_chunks', 'report.apply_patch'],
                'next_work_memory': '对补充后的报告重新运行 QA；如用户要求新增外部事实，先补采来源再改写相关章节。' if wants_external else '对补充后的报告重新运行 QA。',
            }
        if not chunks:
            return {
                'intent': 'answer_only',
                'assistant_answer': '当前 run 暂无报告内容可读取。我可以在报告生成后继续回答具体章节或修改请求。',
                'report_updated': False,
                'actions_taken': [],
                'next_work_memory': '等待报告生成。',
            }
        answer = '我读取了报告相关片段。' + (f'\n\n相关内容摘要：{self._plain_summary(context_text)}' if context_text else '')
        if wants_external and not corpus_refs and request.allow_web_collect:
            answer += '\n\n你问到的部分需要新增外部证据；当前未检索到可用语料，因此不直接编造结论。'
        return {
            'intent': 'answer_only',
            'assistant_answer': answer,
            'report_updated': False,
            'revised_markdown': '',
            'patch_summary': '',
            'actions_taken': ['report.get_chunks'],
            'next_work_memory': '如需把回答沉淀进报告，下一轮可转为 edit_report 并指定章节。',
        }

    def _apply_report_revision(
        self,
        *,
        state: RunState,
        conversation_id: str,
        turn_id: str,
        before_markdown: str,
        after_markdown: str,
        patch_summary: str,
        reason: str,
        source_refs: list[str],
    ) -> str:
        before_hash = hashlib.sha1(before_markdown.encode('utf-8')).hexdigest()
        after_hash = hashlib.sha1(after_markdown.encode('utf-8')).hexdigest()
        if state.report is None:
            return ''
        state.report.markdown = after_markdown
        state.report.html = ''
        if isinstance(state.planner_meta, dict):
            state.planner_meta['last_qa_checked'] = False
            state.planner_meta['last_qa_passed'] = False
            state.planner_meta['last_qa_issue_count'] = 0
        self.store.save_state(state)
        revision = self.store.save_report_revision(
            run_id=state.run_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            before_hash=before_hash,
            after_hash=after_hash,
            patch_summary=patch_summary,
            reason=reason,
            source_refs=source_refs,
        )
        self._emit_event(
            state,
            'chat_report_patched',
            {
                'conversation_id': conversation_id,
                'turn_id': turn_id,
                'revision_id': revision['revision_id'],
                'patch_summary': patch_summary,
            },
        )
        return str(revision['revision_id'])

    def _update_turn_progress(
        self,
        turn_id: str,
        *,
        assistant_answer: str,
        actions_taken: list[str] | None = None,
        source_refs: list[str] | None = None,
    ) -> None:
        self.store.update_conversation_turn(
            turn_id=turn_id,
            status='running',
            result={
                'assistant_answer': assistant_answer,
                'actions_taken': actions_taken or [],
                'report_updated': False,
                'report_revision_id': '',
                'source_refs': source_refs or [],
            },
        )

    def _emit_turn_stream(self, turn_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.workflow.chat_stream_broker.publish(turn_id, event_type, payload)

    def _safe_revised_markdown(
        self,
        *,
        before_markdown: str,
        candidate_markdown: str,
        request_message: str,
        selected_chunks: list[ReportChunk],
        corpus_refs: list[dict[str, Any]],
    ) -> str:
        before = before_markdown.strip()
        candidate = candidate_markdown.strip()
        if not before:
            return candidate
        if self._looks_like_full_safe_report(before, candidate):
            return candidate
        context_text = '\n\n'.join(chunk.text for chunk in selected_chunks[:3]).strip()
        addition = self._build_appendix(request_message, context_text, corpus_refs)
        return self._append_conversation_section(before, addition)

    def _looks_like_full_safe_report(self, before_markdown: str, candidate_markdown: str) -> bool:
        if not candidate_markdown.strip():
            return False
        if len(candidate_markdown) < max(800, int(len(before_markdown) * 0.75)):
            return False
        before_headings = re.findall(r'(?m)^#{1,6}\s+(.+?)\s*$', before_markdown)
        candidate_headings = set(re.findall(r'(?m)^#{1,6}\s+(.+?)\s*$', candidate_markdown))
        if before_headings:
            kept = sum(1 for heading in before_headings if heading in candidate_headings)
            if kept / max(1, len(before_headings)) < 0.7:
                return False
        return True

    def _format_assistant_answer(
        self,
        *,
        base_answer: str,
        actions_taken: list[str],
        report_updated: bool,
        patch_summary: str,
        source_refs: list[str],
        web_refs: list[dict[str, Any]] | None = None,
    ) -> str:
        lines = [base_answer.strip() or '已完成本轮报告追问处理。']
        readable_actions = {
            'report.get_chunks': '读取命中的报告分片',
            'corpus.search': '检索相关语料/公开证据',
            'report.apply_patch': '将修订写回报告 Markdown',
        }
        if actions_taken:
            lines.append('')
            lines.append('本轮操作：' + '；'.join(readable_actions.get(item, item) for item in actions_taken))
        if report_updated:
            summary = patch_summary.strip() or '已根据本轮对话补充报告内容'
            lines.append(f'报告已更新：{summary}。你可以在报告卡片中打开或下载最新 Markdown。')
        else:
            lines.append('报告未自动覆盖原文；如需要写入报告，我会基于证据做保守补充。')
        if source_refs:
            lines.append(f'引用/依据：{len(source_refs)} 条，包含 {", ".join(source_refs[:3])}。')
        return '\n'.join(lines)

    def _format_answer_response(
        self,
        *,
        base_answer: str,
        actions_taken: list[str],
        report_updated: bool,
        patch_summary: str,
        source_refs: list[str],
        web_refs: list[dict[str, Any]] | None = None,
    ) -> str:
        lines = [base_answer.strip() or '已完成本轮追问处理。']
        readable_actions = {
            'report.get_chunks': '读取命中的报告分片',
            'corpus.search': '检索本地公开语料',
            'web.search': '检索公开网页',
            'web.fetch': '抓取公开网页',
            'web.extract': '抽取网页正文',
            'report.apply_patch': '将修订写回报告 Markdown',
        }
        if actions_taken:
            lines.append('')
            lines.append('本轮操作：' + '；'.join(readable_actions.get(item, item) for item in actions_taken))
        if report_updated:
            summary = patch_summary.strip() or '已根据本轮对话补充报告内容'
            lines.append(f'报告已更新：{summary}。你可以在报告卡片中打开或下载最新 Markdown。')
        if source_refs:
            lines.append(f'引用/依据：{len(source_refs)} 条，包含 {", ".join(source_refs[:3])}。')
        return '\n'.join(lines)

    def _format_answer_response_v2(
        self,
        *,
        base_answer: str,
        actions_taken: list[str],
        report_updated: bool,
        patch_summary: str,
        source_refs: list[str],
        web_refs: list[dict[str, Any]] | None = None,
    ) -> str:
        lines = [base_answer.strip() or '已完成本轮追问处理。']
        readable_actions = {
            'report.get_chunks': '读取命中的报告分片',
            'corpus.search': '检索本地公开语料',
            'web.search': '检索公开网页',
            'web.fetch': '抓取公开网页',
            'web.extract': '抽取网页正文',
            'report.apply_patch': '将修订写回报告 Markdown',
        }
        if actions_taken:
            lines.append('')
            lines.append('本轮操作：' + '；'.join(readable_actions.get(item, item) for item in actions_taken))
        if report_updated:
            summary = patch_summary.strip() or '已根据本轮对话补充报告内容'
            lines.append(f'报告已更新：{summary}。你可以在报告卡片中打开或下载最新 Markdown。')
        grouped = self._group_source_refs(source_refs, web_refs=web_refs)
        if any(grouped.values()):
            lines.append('')
            lines.append('本轮依据：')
            lines.append(f'- 报告上下文：{len(grouped["report"])} 条')
            lines.append(f'- 新采集网页：{len(grouped["web"])} 条')
            lines.append(f'- 本地语料：{len(grouped["corpus"])} 条')
            if grouped['web']:
                lines.append('')
                lines.append('新采集网页：')
                for index, url in enumerate(grouped['web'], start=1):
                    lines.append(f'{index}. {url}')
            if grouped['corpus']:
                lines.append('')
                lines.append('本地语料：')
                for index, ref in enumerate(grouped['corpus'][:5], start=1):
                    lines.append(f'{index}. {ref}')
            if grouped['report']:
                lines.append('')
                lines.append('报告上下文：')
                for index, ref in enumerate(grouped['report'][:5], start=1):
                    lines.append(f'{index}. {ref}')
        return '\n'.join(lines)

    @staticmethod
    def _group_source_refs(source_refs: list[str], *, web_refs: list[dict[str, Any]] | None = None) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {'report': [], 'web': [], 'corpus': []}
        seen: set[str] = set()
        collected_web_urls = {
            str(item.get('source_url', '') or '').strip()
            for item in (web_refs or [])
            if str(item.get('source_url', '') or '').strip()
        }
        for raw_ref in source_refs:
            ref = str(raw_ref or '').strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            lowered = ref.lower()
            if lowered.startswith('report:'):
                grouped['report'].append(ref)
            elif ref in collected_web_urls:
                grouped['web'].append(ref)
            else:
                grouped['corpus'].append(ref)
        return grouped

    def _format_answer_response_v3(
        self,
        *,
        base_answer: str,
        actions_taken: list[str],
        report_updated: bool,
        patch_summary: str,
        source_refs: list[str],
        web_refs: list[dict[str, Any]] | None = None,
    ) -> str:
        lines = [base_answer.strip() or '\u5df2\u5b8c\u6210\u672c\u8f6e\u8ffd\u95ee\u5904\u7406\u3002']
        readable_actions = {
            'report.get_chunks': '\u8bfb\u53d6\u547d\u4e2d\u7684\u62a5\u544a\u5206\u7247',
            'corpus.search': '\u68c0\u7d22\u672c\u5730\u516c\u5f00\u8bed\u6599',
            'web.search': '\u68c0\u7d22\u516c\u5f00\u7f51\u9875',
            'web.fetch': '\u6293\u53d6\u516c\u5f00\u7f51\u9875',
            'web.extract': '\u62bd\u53d6\u7f51\u9875\u6b63\u6587',
            'report.apply_patch': '\u5c06\u4fee\u8ba2\u5199\u56de\u62a5\u544a Markdown',
        }
        if actions_taken:
            lines.append('')
            lines.append('\u672c\u8f6e\u64cd\u4f5c\uff1a' + '\uff1b'.join(readable_actions.get(item, item) for item in actions_taken))
        if report_updated:
            summary = patch_summary.strip() or '\u5df2\u6839\u636e\u672c\u8f6e\u5bf9\u8bdd\u8865\u5145\u62a5\u544a\u5185\u5bb9'
            lines.append(f'\u62a5\u544a\u5df2\u66f4\u65b0\uff1a{summary}\u3002\u4f60\u53ef\u4ee5\u5728\u62a5\u544a\u5361\u7247\u4e2d\u6253\u5f00\u6216\u4e0b\u8f7d\u6700\u65b0 Markdown\u3002')
        grouped = self._group_source_refs(source_refs, web_refs=web_refs)
        if any(grouped.values()):
            lines.append('')
            lines.append('\u672c\u8f6e\u4f9d\u636e\uff1a')
            lines.append(f'- \u62a5\u544a\u4e0a\u4e0b\u6587\uff1a{len(grouped["report"])} \u6761')
            lines.append(f'- \u65b0\u91c7\u96c6\u7f51\u9875\uff1a{len(grouped["web"])} \u6761')
            lines.append(f'- \u672c\u5730\u8bed\u6599\uff1a{len(grouped["corpus"])} \u6761')
            if grouped['web']:
                lines.append('')
                lines.append('\u65b0\u91c7\u96c6\u7f51\u9875\uff1a')
                for index, url in enumerate(grouped['web'], start=1):
                    lines.append(f'{index}. {url}')
            if grouped['corpus']:
                lines.append('')
                lines.append('\u672c\u5730\u8bed\u6599\uff1a')
                for index, ref in enumerate(grouped['corpus'][:5], start=1):
                    lines.append(f'{index}. {ref}')
            if grouped['report']:
                lines.append('')
                lines.append('\u62a5\u544a\u4e0a\u4e0b\u6587\uff1a')
                for index, ref in enumerate(grouped['report'][:5], start=1):
                    lines.append(f'{index}. {ref}')
        return '\n'.join(lines)

    def _allows_report_update(self, request: ChatTurnRequest) -> bool:
        if request.mode == 'edit_report':
            return True
        if request.mode != 'auto':
            return False
        text = str(request.message or '').lower()
        explicit_update_tokens = (
            '写入报告', '写到报告', '写回报告', '修改报告', '更新报告', '改写报告', '保存到报告', '同步到报告',
            '应用到报告', '覆盖报告', '更新markdown', '写入markdown', '修改markdown', '保存markdown',
            'edit report', 'update report', 'modify report', 'revise report', 'apply to report', 'save to report',
            'write to report', 'update markdown', 'edit markdown',
        )
        return any(token in text for token in explicit_update_tokens)

    def _normalize_actions(self, raw_actions: Any) -> list[str]:
        allowed = {'report.get_chunks', 'corpus.search', 'web.search', 'web.fetch', 'web.extract', 'report.apply_patch'}
        if not isinstance(raw_actions, list):
            return []
        actions: list[str] = []
        for item in raw_actions:
            action = str(item).strip()
            if action in allowed and action not in actions:
                actions.append(action)
        return actions

    def _decide_web_collect(
        self,
        *,
        state: RunState,
        request: ChatTurnRequest,
        selected_chunks: list[ReportChunk],
        corpus_refs: list[dict[str, Any]],
        memory: dict[str, Any],
    ) -> dict[str, Any]:
        if not request.allow_web_collect:
            return {
                'needs_web_collect': False,
                'queries': [],
                'reason': 'web_collect_disabled',
                'decision_source': 'disabled',
            }
        payload = {
            'run_id': state.run_id,
            'industry': state.industry,
            'competitors': state.competitors,
            'user_message': request.message,
            'mode': request.mode,
            'memory': {
                'short_window': memory.get('short_window', []),
                'mid_summary': memory.get('mid_summary', ''),
                'next_work_memory': memory.get('next_work_memory', ''),
            },
            'report_chunks': [
                {'chunk_id': item.chunk_id, 'heading_path': item.heading_path, 'text_excerpt': item.text[:1200]}
                for item in selected_chunks[:5]
            ],
            'corpus_refs': [
                {
                    'title': item.get('title', ''),
                    'source_url': item.get('source_url', ''),
                    'summary': item.get('summary', ''),
                }
                for item in corpus_refs[:5]
            ],
            'decision_rules': {
                'collect_when_user_asks_for_more_public_facts_than_current_context_contains': True,
                'collect_when_user_asks_for_latest_sources_official_pages_prices_advantages_or_differentiators': True,
                'do_not_collect_when_report_and_corpus_are_sufficient_to_answer_directly': True,
                'return_one_or_two_targeted_queries': True,
            },
        }
        system_prompt = (
            'You are the manager agent for a multi-turn competitor-analysis chat. '
            'Decide whether the current turn needs external web collection before the answer LLM responds. '
            'Use the user message, conversation memory, selected report chunks, and local corpus refs. '
            'If the user asks to supplement more advantages, differentiators, latest facts, official/public sources, pricing changes, or evidence not covered by the provided context, set needs_web_collect=true. '
            'If the provided report_chunks and corpus_refs are already enough to answer the user directly, set needs_web_collect=false. '
            'Return strict JSON only: needs_web_collect boolean, queries array of 1-2 concise search queries, reason string. '
            'Do not answer the user here.'
        )
        try:
            result = self.workflow.agent_llm.invoke_json(
                trace_name='report_conversation_web_collect_decision',
                system_prompt=system_prompt,
                user_payload=payload,
                metadata={'run_id': state.run_id, 'node_name': 'chat', 'agent_name': 'ReportConversationManager'},
            )
            if isinstance(result, dict):
                queries = [str(item).strip() for item in result.get('queries', []) if str(item).strip()] if isinstance(result.get('queries', []), list) else []
                if not queries and bool(result.get('needs_web_collect', False)):
                    queries = self._web_search_queries(state=state, message=request.message)
                return {
                    'needs_web_collect': bool(result.get('needs_web_collect', False)),
                    'queries': queries[:2],
                    'reason': str(result.get('reason', '') or '').strip(),
                    'decision_source': 'llm',
                }
        except LLMCallError:
            pass
        except Exception:
            pass
        fallback = self._needs_web_collect(
            message=request.message,
            selected_chunks=selected_chunks,
            corpus_refs=corpus_refs,
            allow_web_collect=request.allow_web_collect,
        )
        return {
            'needs_web_collect': fallback,
            'queries': self._web_search_queries(state=state, message=request.message) if fallback else [],
            'reason': 'llm_decision_unavailable_fallback_rule_used',
            'decision_source': 'fallback',
        }

    def _needs_web_collect(
        self,
        *,
        message: str,
        selected_chunks: list[ReportChunk],
        corpus_refs: list[dict[str, Any]],
        allow_web_collect: bool,
    ) -> bool:
        if not allow_web_collect:
            return False
        text = str(message or '').lower()
        external_tokens = (
            '最新', '官网', '公开证据', '公开资料', '网页', '来源', '引用', '证据', '采集', '爬取',
            '价格变化', '定价变化', 'source', 'sources', 'web', 'website', 'official', 'latest',
            'recent', 'pricing change',
        )
        if any(token in text for token in external_tokens):
            return True
        return not selected_chunks and not corpus_refs

    def _progress_actions(
        self,
        *,
        selected_chunks: list[ReportChunk],
        corpus_refs: list[dict[str, Any]],
        web_refs: list[dict[str, Any]],
    ) -> list[str]:
        actions: list[str] = []
        if selected_chunks:
            actions.append('report.get_chunks')
        if corpus_refs:
            actions.append('corpus.search')
        if web_refs:
            actions.extend(['web.search', 'web.fetch', 'web.extract'])
        return actions or ['report.get_chunks']

    def _collect_web_refs(self, *, state: RunState, conversation_id: str, turn_id: str, message: str, queries: list[str] | None = None) -> list[dict[str, Any]]:
        router = getattr(self.workflow, 'tool_router', None)
        if router is None:
            self._emit_event(state, 'chat.web_search.failed', {'conversation_id': conversation_id, 'turn_id': turn_id, 'error': 'tool_router_missing'})
            return []
        queries = queries or self._web_search_queries(state=state, message=message)
        web_refs: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for query in queries[:2]:
            self._emit_event(state, 'chat.web_search.started', {'conversation_id': conversation_id, 'turn_id': turn_id, 'query': query})
            try:
                search_result = router.invoke(
                    ToolRequest(
                        name='web.search',
                        args={'query': query, 'max_results': 5},
                        metadata={'run_id': state.run_id, 'node_name': 'chat', 'agent_name': 'ReportConversationManager', 'trace_name': 'chat.web_search'},
                    )
                )
            except Exception as exc:
                self._emit_event(state, 'chat.web_search.failed', {'conversation_id': conversation_id, 'turn_id': turn_id, 'query': query, 'error': str(exc)})
                continue
            hits = search_result.output.get('hits', []) if search_result.ok else []
            if not isinstance(hits, list):
                hits = []
            self._emit_event(
                state,
                'chat.web_search.completed',
                {'conversation_id': conversation_id, 'turn_id': turn_id, 'query': query, 'result_count': len(hits), 'provider': search_result.provider},
            )
            for hit in hits:
                if len(web_refs) >= 3:
                    return web_refs
                if not isinstance(hit, dict):
                    continue
                url = str(hit.get('url') or hit.get('source_url') or '').strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                fetched = self._fetch_web_ref(state=state, conversation_id=conversation_id, turn_id=turn_id, hit=hit, url=url)
                if fetched is not None:
                    web_refs.append(fetched)
        return web_refs

    def _fetch_web_ref(self, *, state: RunState, conversation_id: str, turn_id: str, hit: dict[str, Any], url: str) -> dict[str, Any] | None:
        router = getattr(self.workflow, 'tool_router', None)
        if router is None:
            return None
        title = str(hit.get('title') or hit.get('name') or url).strip()
        snippet = str(hit.get('snippet') or hit.get('summary') or '').strip()
        self._emit_event(state, 'chat.web_fetch.started', {'conversation_id': conversation_id, 'turn_id': turn_id, 'url': url})
        try:
            fetch_result = router.invoke(
                ToolRequest(
                    name='web.fetch',
                    args={'url': url},
                    metadata={'run_id': state.run_id, 'node_name': 'chat', 'agent_name': 'ReportConversationManager', 'trace_name': 'chat.web_fetch'},
                )
            )
        except Exception as exc:
            self._emit_event(state, 'chat.web_fetch.failed', {'conversation_id': conversation_id, 'turn_id': turn_id, 'url': url, 'error': str(exc)})
            return None
        if not fetch_result.ok:
            self._emit_event(state, 'chat.web_fetch.failed', {'conversation_id': conversation_id, 'turn_id': turn_id, 'url': url, 'error': fetch_result.error_message or fetch_result.error_code})
            return None
        content = str(fetch_result.output.get('content', '') or '')
        try:
            extract_result = router.invoke(
                ToolRequest(
                    name='web.extract',
                    args={'content': content[:12000], 'title': title, 'snippet': snippet},
                    metadata={'run_id': state.run_id, 'node_name': 'chat', 'agent_name': 'ReportConversationManager', 'trace_name': 'chat.web_extract'},
                )
            )
        except Exception as exc:
            self._emit_event(state, 'chat.web_fetch.failed', {'conversation_id': conversation_id, 'turn_id': turn_id, 'url': url, 'error': str(exc)})
            return None
        if not extract_result.ok:
            self._emit_event(state, 'chat.web_fetch.failed', {'conversation_id': conversation_id, 'turn_id': turn_id, 'url': url, 'error': extract_result.error_message or extract_result.error_code})
            return None
        sanitized = str(extract_result.output.get('sanitized', '') or '').strip()
        excerpt = self._plain_summary(sanitized or content)
        self._emit_event(state, 'chat.web_fetch.completed', {'conversation_id': conversation_id, 'turn_id': turn_id, 'url': url, 'provider': fetch_result.provider})
        return {
            'title': title,
            'source_url': url,
            'snippet': snippet[:500],
            'summary': excerpt,
            'content_excerpt': excerpt,
            'provider': fetch_result.provider or str(hit.get('provider', '') or ''),
        }

    def _web_search_queries(self, *, state: RunState, message: str) -> list[str]:
        base = re.sub(r'\s+', ' ', str(message or '')).strip()
        return [base] if base else []

    def _search_corpus_refs(self, state: RunState, message: str) -> list[dict[str, Any]]:
        keywords = [token for token in re.findall(r'[\w\u4e00-\u9fff]+', message or '') if len(token) >= 2][:6]
        if not keywords:
            return []
        try:
            return self.store.search_comparison_corpus(industry=state.industry, keywords=keywords, limit=4)
        except Exception:
            return []

    def _get_report_chunks(self, *, run_id: str, markdown: str) -> list[ReportChunk]:
        report_hash = hashlib.sha1(str(markdown or '').encode('utf-8')).hexdigest()
        cache = getattr(self.workflow, 'cache', None)
        if cache is not None:
            cached = cache.get_report_chunks(run_id, report_hash)
            if isinstance(cached, list):
                chunks: list[ReportChunk] = []
                for item in cached:
                    if not isinstance(item, dict):
                        continue
                    try:
                        chunks.append(ReportChunk(**item))
                    except Exception:
                        continue
                if chunks:
                    return chunks
        chunks = split_report_chunks(markdown)
        if cache is not None:
            cache.set_report_chunks(run_id, report_hash, [item.__dict__ for item in chunks])
        return chunks

    def _emit_event(self, state: RunState, event_type: str, payload: dict[str, Any]) -> None:
        log_run_output(
            state.run_id,
            f"[{datetime.now().strftime('%H:%M:%S')}] EVENT: chat -> {event_type} "
            f"(attempt={state.attempt}, status={state.status}, evidences={len(state.evidences)}, findings={len(state.findings)})",
        )
        self.store.append_event(EventRecord(run_id=state.run_id, stage=StageName.draft, event_type=event_type, payload=payload))

    def _message_needs_public_evidence(self, message: str) -> bool:
        return any(token in message for token in ('公开证据', '证据', '公开资料', '采集', '爬取', '网页', 'source', 'web'))

    def _append_conversation_section(self, markdown: str, addition: str) -> str:
        base = markdown.rstrip()
        heading = '## 对话补充'
        if heading in base:
            return f'{base}\n\n{addition}\n'
        return f'{base}\n\n{heading}\n\n{addition}\n'

    def _build_appendix(self, message: str, context_text: str, corpus_refs: list[dict[str, Any]]) -> str:
        lines = [f'- 用户改进点：{message.strip()}']
        if context_text:
            lines.append(f'- 依据当前报告片段：{self._plain_summary(context_text)}')
        if corpus_refs:
            refs = '；'.join(str(item.get('title') or item.get('source_url') or item.get('corpus_id')) for item in corpus_refs[:3])
            lines.append(f'- 可用来源：{refs}')
        else:
            lines.append('- 外部事实状态：本轮未新增已验证外部证据；涉及新事实的结论需后续采集确认。')
        return '\n'.join(lines)

    def _plain_summary(self, text: str) -> str:
        clean = re.sub(r'[#>*_\-\|`]+', ' ', text)
        clean = re.sub(r'\s+', ' ', clean).strip()
        return clean[:420]
