from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import AppConfig
from app.core.models import LLMCallTrace
from app.core.storage import SQLiteStore
from app.core.tools import ToolRequest, ToolRouter, parse_tool_call_turn, tool_specs_for_prompt
from app.core.tracing_factory import get_tracing_runtime

logger = logging.getLogger(__name__)


@dataclass
class LLMCallError(RuntimeError):
    reason: str
    message: str
    attempt_count: int = 0
    retry_count_used: int = 0

    def __str__(self) -> str:
        return self.message


class AgentLLMClient:
    def __init__(self, config: AppConfig, store: SQLiteStore | None = None, tool_router: ToolRouter | None = None):
        self.config = config
        self.store = store
        self.tool_router = tool_router
        self.hook_registry = None

    def _emit_hook(
        self,
        hook_point: str,
        *,
        metadata: dict[str, Any],
        trace_name: str,
        payload: dict[str, Any],
        error: dict[str, Any] | None = None,
    ) -> None:
        if self.hook_registry is None:
            return
        try:
            from app.core.hooks import HookContext

            self.hook_registry.emit(
                hook_point,
                HookContext(
                    hook_point=hook_point,
                    run_id=str(metadata.get('run_id', '') or ''),
                    attempt=int(metadata.get('attempt', 0) or 0),
                    stage=str(metadata.get('node_name', '') or ''),
                    agent_name=str(metadata.get('agent_name', '') or ''),
                    trace_name=trace_name,
                    payload=payload,
                    error=error,
                ),
            )
        except Exception:
            return

    def enabled(self) -> bool:
        return bool(self.config.openai_api_key and self.config.openai_base_url and self.config.openai_model)

    def invoke_json(
        self,
        *,
        trace_name: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        metadata: dict[str, Any],
        network_retries: int | None = None,
    ) -> dict[str, Any]:
        if self.tool_router is not None and not bool(metadata.get('_via_tool', False)):
            routed = self.tool_router.invoke(
                ToolRequest(
                    name='llm.invoke_json',
                    args={
                        'trace_name': trace_name,
                        'system_prompt': system_prompt,
                        'user_payload': user_payload,
                        'metadata': {**metadata, '_via_tool': True},
                    },
                    max_retries=network_retries if network_retries is not None else self.config.agent_llm_retry_count,
                    metadata={'group': 'llm'},
                )
            )
            if routed.ok:
                parsed = routed.output.get('parsed', {})
                if isinstance(parsed, dict):
                    return parsed
            raise LLMCallError(reason=routed.error_code or 'llm_invoke_failed', message=routed.error_message or 'llm_invoke_failed')

        if not self.enabled():
            raise LLMCallError(
                reason='llm_not_configured',
                message='LLM is not configured: missing OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL',
                attempt_count=0,
                retry_count_used=0,
            )

        messages = [
            {
                'role': 'system',
                'content': (
                    f'{system_prompt}\n'
                    'Return only one valid JSON object. '
                    'Do not include markdown code fences. '
                    'Do not include explanation text before or after JSON.'
                ),
            },
            {'role': 'user', 'content': json.dumps(user_payload, ensure_ascii=False)},
        ]
        return self._invoke_json_with_messages(
            trace_name=trace_name,
            system_prompt=system_prompt,
            user_payload=user_payload,
            metadata=metadata,
            messages=messages,
            network_retries=network_retries,
            temperature=0.2,
        )

    def invoke_json_multimodal(
        self,
        *,
        trace_name: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        user_text: str,
        image_data_url: str,
        metadata: dict[str, Any],
        network_retries: int | None = None,
    ) -> dict[str, Any]:
        if not image_data_url.startswith('data:image'):
            raise LLMCallError(
                reason='invalid_image_data_url',
                message='image_data_url must be a valid data:image URL',
                attempt_count=0,
                retry_count_used=0,
            )
        messages = [
            {
                'role': 'system',
                'content': (
                    f'{system_prompt}\n'
                    'Return only one valid JSON object. '
                    'Do not include markdown code fences. '
                    'Do not include explanation text before or after JSON.'
                ),
            },
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': user_text},
                    {'type': 'image_url', 'image_url': {'url': image_data_url}},
                ],
            },
        ]
        return self._invoke_json_with_messages(
            trace_name=trace_name,
            system_prompt=system_prompt,
            user_payload=user_payload,
            metadata=metadata,
            messages=messages,
            network_retries=network_retries,
            temperature=0.0,
        )

    def invoke_json_with_tools(
        self,
        *,
        trace_name: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        metadata: dict[str, Any],
        tool_names: list[str],
        max_tool_rounds: int = 4,
        tool_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.tool_router is None:
            return self.invoke_json(
                trace_name=trace_name,
                system_prompt=system_prompt,
                user_payload=user_payload,
                metadata=metadata,
            )
        policy = dict(tool_policy or {})
        disable_tools = bool(policy.get('disable_tools', False))
        if disable_tools:
            return self.invoke_json(
                trace_name=trace_name,
                system_prompt=system_prompt,
                user_payload=user_payload,
                metadata=metadata,
            )
        effective_rounds = int(policy.get('max_tool_rounds', max_tool_rounds) or max_tool_rounds)
        effective_rounds = max(1, effective_rounds)
        denied_tools = {str(item).strip() for item in policy.get('denied_tools', []) if str(item).strip()} if isinstance(policy.get('denied_tools', []), list) else set()
        allowed_tool_names = [name for name in tool_names if name not in denied_tools]

        specs = []
        for name in allowed_tool_names:
            try:
                spec = self.tool_router.registry.get_spec(name)
            except Exception:
                continue
            specs.append(
                {
                    'name': spec.name,
                    'group': spec.group,
                    'description': spec.description,
                    'schema': spec.schema,
                }
            )
        if not specs:
            return self.invoke_json(
                trace_name=trace_name,
                system_prompt=system_prompt,
                user_payload=user_payload,
                metadata=metadata,
            )

        protocol_prompt = (
            f"{system_prompt}\n\n"
            "You can call tools before final answer.\n"
            "Return strict JSON only with this schema:\n"
            "{\"tool_calls\":[{\"name\":\"tool.name\",\"arguments\":{}}],\"final_output\":{}}\n"
            "- If you need tools, set tool_calls and set final_output to null.\n"
            "- If done, set tool_calls to [] and put answer in final_output.\n"
            "Available tools:\n"
            f"{tool_specs_for_prompt(specs)}"
        )

        history: list[dict[str, Any]] = []
        consecutive_empty_calls = 0
        for round_index in range(1, effective_rounds + 1):
            payload = {
                'task': user_payload,
                'tool_history': history,
                'round': round_index,
                'max_rounds': effective_rounds,
            }
            model_result = self.invoke_json(
                trace_name=f"{trace_name}.tool_round",
                system_prompt=protocol_prompt,
                user_payload=payload,
                metadata={**metadata, '_via_tool': True, 'tool_round': round_index},
            )
            turn = parse_tool_call_turn(model_result)
            if turn.final_output is not None and not turn.tool_calls:
                return turn.final_output
            if not turn.tool_calls:
                consecutive_empty_calls += 1
                if consecutive_empty_calls >= 2:
                    raise LLMCallError(
                        reason='tool_protocol_error',
                        message='consecutive empty tool_calls without final_output',
                        attempt_count=round_index,
                        retry_count_used=0,
                    )
                return model_result if isinstance(model_result, dict) else {}

            round_calls: list[dict[str, Any]] = []
            for call in turn.tool_calls:
                if call.name not in set(allowed_tool_names):
                    round_calls.append(
                        {
                            'name': call.name,
                            'arguments': call.arguments,
                            'ok': False,
                            'output': {},
                            'error_code': 'forbidden_tool',
                            'error_message': f'tool_not_allowed_for_role: {call.name}',
                        }
                    )
                    continue
                tool_result = self.tool_router.invoke(
                    ToolRequest(
                        name=call.name,
                        args=call.arguments,
                        metadata={
                            'group': 'tool_call_protocol',
                            'allowed_tools': allowed_tool_names,
                            'agent_name': metadata.get('agent_name', ''),
                            'trace_name': trace_name,
                            'tool_round': round_index,
                            **metadata,
                        },
                    )
                )
                round_calls.append(
                    {
                        'name': call.name,
                        'arguments': call.arguments,
                        'ok': tool_result.ok,
                        'output': tool_result.output,
                        'error_code': tool_result.error_code,
                        'error_message': tool_result.error_message,
                    }
                )
            history.append({'round': round_index, 'tool_calls': round_calls})

        if bool(policy.get('fallback_to_plain_json', True)):
            return self.invoke_json(
                trace_name=trace_name,
                system_prompt=system_prompt,
                user_payload=user_payload,
                metadata={**metadata, '_via_tool': True, 'tool_protocol_fallback': True},
            )
        raise LLMCallError(
            reason='tool_round_exhausted',
            message=f'tool call rounds exhausted: {effective_rounds}',
            attempt_count=effective_rounds,
            retry_count_used=0,
        )

    def _invoke_json_with_messages(
        self,
        *,
        trace_name: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        metadata: dict[str, Any],
        messages: list[dict[str, Any]],
        network_retries: int | None,
        temperature: float,
    ) -> dict[str, Any]:
        self._emit_hook(
            'before_llm',
            metadata=metadata,
            trace_name=trace_name,
            payload={'user_payload': user_payload, 'temperature': temperature},
        )
        retries = self.config.agent_llm_retry_count if network_retries is None else max(0, network_retries)
        attempts = retries + 1

        runtime = get_tracing_runtime()
        base_url = self.config.openai_base_url.rstrip('/')
        url = f'{base_url}/chat/completions'
        payload = {
            'model': self.config.openai_model,
            'messages': messages,
            'temperature': temperature,
        }

        last_exc: Exception | None = None
        last_reason = 'unknown'

        trace_ctx = _trace_ctx(
            name=trace_name,
            inputs=user_payload,
            metadata={'model': self.config.openai_model, **metadata},
            project=runtime.project,
            client=runtime.client,
            enabled=runtime.langsmith_enabled,
        )

        with trace_ctx:
            for idx in range(attempts):
                started_at = time.time()
                try:
                    data = self._post_chat_completion(url=url, payload=payload)
                    choices = data.get('choices', [])
                    if not choices:
                        raise LLMCallError(
                            reason='empty_choices',
                            message='LLM response missing choices',
                            attempt_count=idx + 1,
                            retry_count_used=idx,
                        )
                    content = (choices[0].get('message') or {}).get('content', '{}')
                    try:
                        parsed = _parse_json_content(content)
                    except ValueError:
                        repaired = self._repair_json_response(
                            url=url,
                            raw_content=content,
                            trace_name=trace_name,
                            metadata=metadata,
                        )
                        parsed = _parse_json_content(repaired)
                    self._record_llm_trace(
                        trace_name=trace_name,
                        system_prompt=system_prompt,
                        user_payload=user_payload,
                        metadata=metadata,
                        raw_response=data,
                        parsed_response=parsed,
                        status='completed',
                        latency_ms=int((time.time() - started_at) * 1000),
                    )
                    return parsed
                except LLMCallError as exc:
                    self._record_llm_trace(
                        trace_name=trace_name,
                        system_prompt=system_prompt,
                        user_payload=user_payload,
                        metadata=metadata,
                        raw_response={},
                        parsed_response={},
                        status='failed',
                        latency_ms=int((time.time() - started_at) * 1000),
                        error_reason=exc.reason,
                        error_message=str(exc),
                    )
                    last_exc = exc
                    last_reason = exc.reason
                    if idx < attempts - 1 and _is_retryable_reason(exc.reason):
                        _sleep_backoff(idx, self.config.agent_llm_retry_backoff_ms, self.config.agent_llm_retry_max_backoff_ms)
                        continue
                    break
                except Exception as exc:
                    reason = _classify_error(exc)
                    self._record_llm_trace(
                        trace_name=trace_name,
                        system_prompt=system_prompt,
                        user_payload=user_payload,
                        metadata=metadata,
                        raw_response={},
                        parsed_response={},
                        status='failed',
                        latency_ms=int((time.time() - started_at) * 1000),
                        error_reason=reason,
                        error_message=str(exc),
                    )
                    last_exc = exc
                    last_reason = reason
                    if idx < attempts - 1 and _is_retryable_reason(reason):
                        _sleep_backoff(idx, self.config.agent_llm_retry_backoff_ms, self.config.agent_llm_retry_max_backoff_ms)
                        continue
                    break

        message = f'LLM call failed after {attempts} attempt(s): {last_exc}' if last_exc else 'LLM call failed'
        self._emit_hook(
            'on_error',
            metadata=metadata,
            trace_name=trace_name,
            payload={},
            error={'reason': last_reason, 'message': message},
        )
        raise LLMCallError(reason=last_reason, message=message, attempt_count=attempts, retry_count_used=max(0, attempts - 1))

    def _post_chat_completion(self, *, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.config.openai_api_key}',
                'Accept': 'application/json',
                'Connection': 'close',
            },
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=self.config.request_timeout_seconds) as resp:
            body = resp.read().decode('utf-8', errors='ignore')
        return json.loads(body)

    def _repair_json_response(
        self,
        *,
        url: str,
        raw_content: Any,
        trace_name: str,
        metadata: dict[str, Any],
    ) -> str:
        repair_payload = {
            'model': self.config.openai_model,
            'messages': [
                {
                    'role': 'system',
                    'content': (
                        '你是 JSON 修复助手。'
                        '你的唯一任务是把给定文本修正为一个且仅一个合法 JSON 对象。'
                        '不要补充解释，不要输出 markdown 代码块，不要输出多个 JSON 对象。'
                        '如果原文里已经包含 JSON，请尽量保持原有字段和值，只修复格式问题。'
                    ),
                },
                {
                    'role': 'user',
                    'content': json.dumps(
                        {
                            'task': '请将下面这段模型输出修正为一个合法的 JSON 对象，只返回修正后的 JSON。',
                            'raw_content': str(raw_content or ''),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            'temperature': 0.0,
        }
        repair_data = self._post_chat_completion(url=url, payload=repair_payload)
        choices = repair_data.get('choices', [])
        if not choices:
            raise LLMCallError(
                reason='empty_choices',
                message=f'LLM JSON repair missing choices for {trace_name}',
                attempt_count=1,
                retry_count_used=0,
            )
        return str((choices[0].get('message') or {}).get('content', '{}'))

    def _record_llm_trace(
        self,
        *,
        trace_name: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        metadata: dict[str, Any],
        raw_response: dict[str, Any],
        parsed_response: dict[str, Any],
        status: str,
        latency_ms: int,
        error_reason: str = '',
        error_message: str = '',
    ) -> None:
        if self.store is None:
            return
        usage = raw_response.get('usage', {}) if isinstance(raw_response, dict) else {}
        prompt_tokens, completion_tokens, total_tokens, usage_source, usage_details = _extract_usage(usage)
        finish_reason = ''
        choices = raw_response.get('choices', []) if isinstance(raw_response, dict) else []
        if choices and isinstance(choices[0], dict):
            finish_reason = str(choices[0].get('finish_reason', '') or '')
        trace = LLMCallTrace(
            run_id=str(metadata.get('run_id', '') or ''),
            attempt=int(metadata.get('attempt', 0) or 0),
            node_name=str(metadata.get('node_name', '') or ''),
            agent_name=str(metadata.get('agent_name', '') or ''),
            trace_name=trace_name,
            model=str(metadata.get('model', self.config.openai_model) or self.config.openai_model),
            status='completed' if status == 'completed' else 'failed',
            system_prompt=system_prompt,
            user_payload=user_payload,
            raw_response=raw_response if isinstance(raw_response, dict) else {},
            parsed_response=parsed_response if isinstance(parsed_response, dict) else {},
            error_reason=error_reason,
            error_message=error_message[:2000],
            finish_reason=finish_reason,
            latency_ms=max(0, latency_ms),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            usage_source=usage_source,
            usage_details=usage_details,
            created_at=datetime.now(UTC),
        )
        try:
            self.store.save_llm_call(trace)
        except Exception as exc:
            logger.warning('Failed to persist llm trace %s: %s', trace_name, exc)


class _NullTrace:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _trace_ctx(*, name: str, inputs: dict[str, Any], metadata: dict[str, Any], project: str, client: Any | None, enabled: bool):
    if not enabled or client is None:
        return _NullTrace()
    try:
        from langsmith.run_helpers import trace

        return trace(
            name=name,
            run_type='llm',
            inputs=inputs,
            metadata=metadata,
            project_name=project,
            client=client,
        )
    except Exception as exc:
        logger.warning('LangSmith trace creation failed for %s: %s', name, exc)
        return _NullTrace()


def _sleep_backoff(idx: int, base_ms: int, max_ms: int) -> None:
    delay_ms = min(max_ms, base_ms * (2**idx))
    time.sleep(delay_ms / 1000.0)


def _is_retryable_reason(reason: str) -> bool:
    return reason in {
        'network_timeout',
        'network_reset',
        'http_5xx',
        'http_429',
        'json_decode_error',
        'unknown_network',
    }


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return 'network_timeout'
    if isinstance(exc, socket.timeout):
        return 'network_timeout'
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return 'http_429'
        if 500 <= exc.code <= 599:
            return 'http_5xx'
        return 'http_4xx'
    if isinstance(exc, urllib.error.URLError):
        reason = str(exc.reason).lower()
        if 'timed out' in reason:
            return 'network_timeout'
        if 'reset' in reason or 'closed' in reason or 'eof occurred in violation of protocol' in reason:
            return 'network_reset'
        return 'unknown_network'
    if isinstance(exc, json.JSONDecodeError):
        return 'json_decode_error'
    if isinstance(exc, ValueError):
        return 'json_decode_error'
    return 'unknown'


def _parse_json_content(content: Any) -> dict[str, Any]:
    text = str(content or '').strip()
    if not text:
        raise ValueError('json_parse_failed: empty_response_content')
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(parsed, dict):
            return parsed
        raise ValueError('json_parse_failed: parsed JSON is not an object')

    cleaned = _strip_json_fence(text).strip()
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start != -1 and end != -1 and end >= start:
        candidate = cleaned[start : end + 1]
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError('json_parse_failed: unable to parse valid JSON object from response')


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith('```'):
        stripped = re.sub(r'^```(?:json)?\s*', '', stripped, flags=re.IGNORECASE)
        stripped = re.sub(r'\s*```$', '', stripped)
    return stripped


def _extract_usage(usage: Any) -> tuple[int, int, int, str, dict[str, Any]]:
    if not isinstance(usage, dict):
        return 0, 0, 0, 'missing', {}
    prompt_tokens = int(usage.get('prompt_tokens', 0) or 0)
    completion_tokens = int(usage.get('completion_tokens', 0) or 0)
    total_tokens = int(usage.get('total_tokens', prompt_tokens + completion_tokens) or 0)
    details = {
        key: value
        for key, value in usage.items()
        if key not in {'prompt_tokens', 'completion_tokens', 'total_tokens'}
    }
    source = 'provider' if any([prompt_tokens, completion_tokens, total_tokens, details]) else 'missing'
    return prompt_tokens, completion_tokens, total_tokens, source, details
