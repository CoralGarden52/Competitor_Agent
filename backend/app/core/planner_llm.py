from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

from app.core.config import AppConfig
from app.core.tracing_factory import get_tracing_runtime
from openai import OpenAI

logger = logging.getLogger(__name__)


DEFAULT_SCHEMA_PLAN: list[dict[str, Any]] = [
    {
        'field_name': 'feature_tree',
        'query_templates': [
            '{product} core features',
            '{product} official docs features',
            '{product} use cases',
        ],
        'recommended_sources': ['official', 'docs', 'product_pages'],
        'priority': 1,
    },
    {
        'field_name': 'strengths',
        'query_templates': [
            '{product} strengths review',
            '{product} benchmark comparison advantages',
            '{product} why choose',
        ],
        'recommended_sources': ['review', 'analysis', 'community'],
        'priority': 2,
    },
    {
        'field_name': 'weaknesses',
        'query_templates': [
            '{product} weaknesses limitations',
            '{product} issues complaints',
            '{product} cons review',
        ],
        'recommended_sources': ['review', 'community', 'issues'],
        'priority': 3,
    },
    {
        'field_name': 'pricing_model',
        'query_templates': [
            '{product} pricing plans',
            '{product} enterprise billing',
            '{product} free tier pricing',
        ],
        'recommended_sources': ['official', 'pricing', 'docs'],
        'priority': 4,
    },
    {
        'field_name': 'user_feedback',
        'query_templates': [
            '{product} user feedback 知乎',
            '{product} user reviews reddit',
            '{product} g2 capterra review',
        ],
        'recommended_sources': ['zhihu', 'community', 'review'],
        'priority': 5,
    },
]

CORE_DYNAMIC_FIELDS: list[str] = ['feature_tree', 'strengths', 'weaknesses', 'pricing_model', 'user_feedback']


class PlannerLLMClient:
    def __init__(self, config: AppConfig):
        self.config = config
        self._last_call_status: dict[str, Any] = {
            'success': False,
            'endpoint': '',
            'http_status': None,
            'error': '',
            'attempted_endpoints': [],
        }
        self._step_call_status: dict[str, dict[str, Any]] = {}

    def enabled(self) -> bool:
        return bool(self.config.openai_api_key and self.config.openai_base_url and self.config.openai_model)

    def _chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        base_url = self.config.openai_base_url.rstrip('/')
        endpoint = f'{base_url}/chat/completions' if base_url else ''
        strict_system_prompt = (
            f'{system_prompt}\n'
            'Return only one valid JSON object. '
            'Do not include markdown code fences. '
            'Do not include explanation text before or after JSON.'
        )
        max_attempts = max(1, self.config.planner_llm_retry_count + 1)
        attempted_endpoints: list[str] = []
        last_exc: Exception | None = None
        last_http_status: int | None = None
        last_error_text = 'unknown_error'

        for attempt in range(1, max_attempts + 1):
            if endpoint:
                attempted_endpoints.append(endpoint)
            try:
                client = OpenAI(
                    api_key=self.config.openai_api_key,
                    base_url=self.config.openai_base_url,
                    timeout=self.config.request_timeout_seconds,
                )
                response = client.chat.completions.create(
                    model=self.config.openai_model,
                    messages=[
                        {'role': 'system', 'content': strict_system_prompt},
                        {'role': 'user', 'content': user_prompt},
                    ],
                    temperature=0.2,
                )
                choices = getattr(response, 'choices', None) or []
                if not choices:
                    raise ValueError('empty_choices')
                message = getattr(choices[0], 'message', None)
                content = getattr(message, 'content', '{}') if message is not None else '{}'
                parsed = self._parse_json_content(content)
                if not isinstance(parsed, dict):
                    raise ValueError('json_parse_failed: parsed JSON is not an object')
                self._last_call_status = {
                    'success': True,
                    'endpoint': endpoint,
                    'http_status': 200,
                    'error': '',
                    'attempted_endpoints': attempted_endpoints,
                }
                return parsed
            except Exception as exc:
                http_status = getattr(exc, 'status_code', None)
                error_text = str(exc) or exc.__class__.__name__
                if 'json_parse_failed' not in error_text and isinstance(exc, (json.JSONDecodeError, ValueError, TypeError)):
                    error_text = f'json_parse_failed: {error_text}'
                last_exc = exc
                last_http_status = http_status
                last_error_text = error_text
                if attempt < max_attempts and self._is_retryable_planner_error(exc, http_status=http_status, error_text=error_text):
                    self._planner_retry_sleep(attempt - 1)
                    continue
                break

        error_with_attempt = f'{last_error_text} (attempt={len(attempted_endpoints)}/{max_attempts})'
        self._last_call_status = {
            'success': False,
            'endpoint': endpoint,
            'http_status': last_http_status,
            'error': error_with_attempt,
            'attempted_endpoints': attempted_endpoints,
        }
        raise RuntimeError(f'llm_chat_failed: {error_with_attempt}') from last_exc

    def last_call_status(self) -> dict[str, Any]:
        return dict(self._last_call_status)

    def step_call_status(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._step_call_status.items()}

    def _record_step_status(self, step: str) -> None:
        status = self.last_call_status()
        status['attempt_count'] = len(status.get('attempted_endpoints', []) or [])
        self._step_call_status[step] = status

    def check_health(self) -> dict[str, Any]:
        if not self.enabled():
            return {
                'enabled': False,
                'success': False,
                'reason': 'missing OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL',
                'llm_call_status': self.last_call_status(),
            }
        try:
            _ = self._chat_json('Return strict JSON.', 'Return JSON: {"ok": true}')
            return {
                'enabled': True,
                'success': True,
                'reason': 'llm endpoint reachable',
                'llm_call_status': self.last_call_status(),
            }
        except Exception as exc:
            return {
                'enabled': True,
                'success': False,
                'reason': str(exc),
                'llm_call_status': self.last_call_status(),
            }

    def _trace_llm_call(self, *, name: str, inputs: dict[str, Any]):
        runtime = get_tracing_runtime()
        if not runtime.langsmith_enabled or runtime.client is None:
            return _NullTrace()
        try:
            from langsmith.run_helpers import trace

            return trace(
                name=name,
                run_type='llm',
                project_name=runtime.project,
                inputs=inputs,
                metadata={'model': self.config.openai_model, 'component': 'planner_llm'},
                client=runtime.client,
            )
        except Exception as exc:
            logger.warning('LangSmith trace disabled for this call due to runtime error: %s', exc)
            return _NullTrace()

    def _is_retryable_planner_error(self, exc: Exception, *, http_status: int | None, error_text: str) -> bool:
        if isinstance(exc, (json.JSONDecodeError, ValueError, TypeError)):
            return False
        if http_status is not None:
            if http_status == 429 or http_status >= 500:
                return True
            return False
        text = error_text.lower()
        retryable_markers = (
            'timed out',
            'timeout',
            'connection reset',
            'temporarily unavailable',
            'service unavailable',
            'rate limit',
            'too many requests',
        )
        return any(marker in text for marker in retryable_markers)

    def _planner_retry_sleep(self, retry_index: int) -> None:
        delay_ms = min(
            self.config.planner_llm_retry_max_backoff_ms,
            self.config.planner_llm_retry_backoff_ms * (2**retry_index),
        )
        time.sleep(delay_ms / 1000.0)

    def discover_competitors(self, *, industry: str, user_competitors: list[str]) -> list[str]:
        if not self.enabled():
            return []
        sys_prompt = 'You are a competitor discovery assistant. Return strict JSON.'
        user_prompt = (
            f'industry={industry}\n'
            f'user_competitors={user_competitors}\n'
            'Return JSON: {"competitors": ["name1", "name2", ...]} with at most 8 items.'
        )
        try:
            with self._trace_llm_call(
                name='planner.discover_competitors',
                inputs={'industry': industry, 'user_competitors': user_competitors},
            ):
                result = self._chat_json(sys_prompt, user_prompt)
            competitors = result.get('competitors', [])
            if isinstance(competitors, list):
                return [self._repair_mojibake(str(x).strip()) for x in competitors if str(x).strip()]
            return []
        except Exception:
            return []

    def infer_industry_from_prompt(self, *, prompt: str, industry_hint: str | None = None) -> str:
        hint = (industry_hint or '').strip()
        if hint:
            return hint
        if not self.enabled():
            return 'general'
        sys_prompt = 'You infer industry from research prompt. Return strict JSON.'
        user_prompt = (
            f'prompt={prompt}\n'
            'Return JSON: {"industry":"short_lowercase_label"}'
        )
        try:
            with self._trace_llm_call(name='planner.infer_industry', inputs={'prompt': prompt}):
                result = self._chat_json(sys_prompt, user_prompt)
            self._record_step_status('infer_industry')
            industry = str(result.get('industry', '')).strip().lower()
            return industry or 'general'
        except Exception:
            self._record_step_status('infer_industry')
            return 'general'

    def discover_competitors_grouped(self, *, prompt: str, competitor_hints: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not self.enabled():
            direct = [self._make_candidate(name=x, fit_type='direct', reason='provided hint') for x in competitor_hints if x.strip()]
            return {'direct': direct[:6], 'substitute': []}
        sys_prompt = 'You discover competitors from prompt. Return strict JSON with direct/substitute only.'
        user_prompt = (
            f'prompt={prompt}\n'
            f'competitor_hints={competitor_hints}\n'
            'Return JSON: {"direct":[{"name":"","reason":"","confidence":0.8}],'
            '"substitute":[{"name":"","reason":"","confidence":0.6}]}. '
            'No irrelevant category.'
        )
        try:
            with self._trace_llm_call(name='planner.discover_competitors_grouped', inputs={'prompt': prompt, 'competitor_hints': competitor_hints}):
                result = self._chat_json(sys_prompt, user_prompt)
            self._record_step_status('discover_competitors_grouped')
        except Exception:
            self._record_step_status('discover_competitors_grouped')
            result = {}
        direct = self._clean_candidates(result.get('direct', []), fallback_hints=competitor_hints, default_fit='direct')
        substitute = self._clean_candidates(result.get('substitute', []), fallback_hints=[], default_fit='substitute')
        return {'direct': direct[:8], 'substitute': substitute[:6]}

    def plan_dynamic_schema(self, *, prompt: str, industry: str, candidates: list[str]) -> list[dict[str, Any]]:
        if not self.enabled():
            return self._core_schema_plan_only()
        core_plan = self._core_schema_plan_only()
        extra_plan = self.plan_schema_extensions_from_prompt(prompt=prompt, core_schema_fields=CORE_DYNAMIC_FIELDS, candidate_names=candidates)
        if extra_plan:
            self._record_step_status('plan_dynamic_schema')
            return self._normalize_dynamic_schema(core_plan + extra_plan)
        self._record_step_status('plan_dynamic_schema')
        return core_plan

    def plan_schema_extensions_from_prompt(
        self,
        *,
        prompt: str,
        core_schema_fields: list[str],
        candidate_names: list[str],
    ) -> list[dict[str, Any]]:
        if not self.enabled():
            return []
        sys_prompt = 'You design additional competitor-analysis schema fields. Return strict JSON only.'
        limited_candidates = [str(x).strip() for x in candidate_names if str(x).strip()][: self.config.planner_schema_max_candidates]
        user_prompt = (
            f'user_prompt={prompt}\n'
            f'core_schema_fields={core_schema_fields}\n'
            f'candidate_names={limited_candidates}\n'
            'Add only incremental fields beyond core_schema_fields. '
            'Return JSON: {"extra_schema_fields":[{"field_name":"","query_templates":["{product} ..."],'
            '"recommended_sources":["official"],"priority":1}]}. '
            '0-6 fields; do not repeat any core field names.'
        )
        try:
            with self._trace_llm_call(
                name='planner.plan_schema_extensions',
                inputs={'prompt': prompt, 'core_schema_fields': core_schema_fields, 'candidate_names': limited_candidates},
            ):
                result = self._chat_json(sys_prompt, user_prompt)
            plan = result.get('extra_schema_fields', [])
            if not isinstance(plan, list):
                return []
            return self._normalize_extra_schema(plan, core_schema_fields=core_schema_fields)
        except Exception:
            return []

    def plan_schema(self, *, industry: str, target_product: str, competitors: list[str]) -> list[dict[str, Any]]:
        if not self.enabled():
            return self._core_schema_plan_only()
        sys_prompt = 'You design competitor analysis schema plans. Return strict JSON.'
        user_prompt = (
            f'industry={industry}\n'
            f'target_product={target_product}\n'
            f'competitors={competitors}\n'
            'Return JSON: {"schema_plan":[{"field_name":"", "query_templates":["{product} ..."], "recommended_sources":[""], "priority":1}]}. '
            'Need 6-10 fields.'
        )
        try:
            with self._trace_llm_call(
                name='planner.plan_schema',
                inputs={'industry': industry, 'target_product': target_product, 'competitors': competitors},
            ):
                result = self._chat_json(sys_prompt, user_prompt)
            plan = result.get('schema_plan', [])
            if not isinstance(plan, list) or not plan:
                return self._core_schema_plan_only()
            cleaned: list[dict[str, Any]] = []
            for item in plan:
                if not isinstance(item, dict):
                    continue
                field_name = self._repair_mojibake(str(item.get('field_name', '')).strip())
                if not field_name:
                    continue
                templates = [self._repair_mojibake(str(x).strip()) for x in item.get('query_templates', []) if str(x).strip()]
                if not templates:
                    templates = [f'{{product}} {field_name}']
                sources = [self._repair_mojibake(str(x).strip()) for x in item.get('recommended_sources', []) if str(x).strip()]
                priority = int(item.get('priority', len(cleaned) + 1))
                cleaned.append(
                    {
                        'field_name': field_name,
                        'query_templates': templates[:4],
                        'recommended_sources': sources[:5],
                        'priority': priority,
                    }
                )
            return self._normalize_dynamic_schema(cleaned) if cleaned else self._core_schema_plan_only()
        except Exception:
            return self._core_schema_plan_only()

    def planner_meta(self, *, industry: str, competitors: list[str], schema_plan: list[dict[str, Any]]) -> dict[str, Any]:
        digest = hashlib.sha256(
            json.dumps({'industry': industry, 'competitors': competitors, 'schema_plan': schema_plan}, ensure_ascii=False).encode('utf-8')
        ).hexdigest()[:16]
        return {
            'model': self.config.openai_model,
            'generated_at': datetime.now(UTC).isoformat(),
            'plan_hash': digest,
            'llm_enabled': self.enabled(),
        }

    def _core_schema_plan_only(self) -> list[dict[str, Any]]:
        return self._normalize_dynamic_schema(list(DEFAULT_SCHEMA_PLAN))

    def _normalize_dynamic_schema(self, raw_plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_plan:
            if not isinstance(item, dict):
                continue
            field_name = self._repair_mojibake(str(item.get('field_name', '')).strip().lower())
            if not field_name:
                continue
            if field_name in seen:
                continue
            seen.add(field_name)
            templates = item.get('query_templates', [])
            if not isinstance(templates, list):
                templates = []
            q = [self._repair_mojibake(str(x).strip()) for x in templates if str(x).strip()]
            if not q:
                q = [f'{{product}} {field_name}']
            sources = item.get('recommended_sources', [])
            if not isinstance(sources, list):
                sources = []
            rs = [self._repair_mojibake(str(x).strip().lower()) for x in sources if str(x).strip()]
            priority = int(item.get('priority', len(cleaned) + 1))
            cleaned.append({'field_name': field_name, 'query_templates': q[:4], 'recommended_sources': rs[:5], 'priority': priority})

        # Ensure core fields always exist.
        for field_name in CORE_DYNAMIC_FIELDS:
            if field_name in seen:
                continue
            default_q = '{product} user feedback 知乎 reddit' if field_name == 'user_feedback' else f'{{product}} {field_name}'
            default_sources = ['zhihu', 'community'] if field_name == 'user_feedback' else ['official', 'public_web']
            cleaned.append(
                {'field_name': field_name, 'query_templates': [default_q], 'recommended_sources': default_sources, 'priority': len(cleaned) + 1}
            )

        cleaned.sort(key=lambda x: int(x.get('priority', 999)))
        for index, item in enumerate(cleaned, start=1):
            item['priority'] = index
        return cleaned[:12]

    def _normalize_extra_schema(self, raw_plan: list[dict[str, Any]], *, core_schema_fields: list[str]) -> list[dict[str, Any]]:
        core_set = {str(x).strip().lower() for x in core_schema_fields if str(x).strip()}
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw_plan:
            if not isinstance(item, dict):
                continue
            field_name = self._repair_mojibake(str(item.get('field_name', '')).strip().lower())
            if not field_name or field_name in core_set or field_name in seen:
                continue
            seen.add(field_name)
            templates = item.get('query_templates', [])
            if not isinstance(templates, list):
                templates = []
            q = [self._repair_mojibake(str(x).strip()) for x in templates if str(x).strip()]
            if not q:
                q = [f'{{product}} {field_name}']
            sources = item.get('recommended_sources', [])
            if not isinstance(sources, list):
                sources = []
            rs = [self._repair_mojibake(str(x).strip().lower()) for x in sources if str(x).strip()]
            priority = int(item.get('priority', len(cleaned) + 1))
            cleaned.append({'field_name': field_name, 'query_templates': q[:4], 'recommended_sources': rs[:5], 'priority': priority})
        cleaned.sort(key=lambda x: int(x.get('priority', 999)))
        for index, item in enumerate(cleaned, start=1):
            item['priority'] = index
        return cleaned[:6]

    @staticmethod
    def _make_candidate(*, name: str, fit_type: str, reason: str) -> dict[str, Any]:
        return {'name': name.strip(), 'fit_type': fit_type, 'reason': reason, 'confidence': 0.7}

    def _clean_candidates(self, raw: Any, *, fallback_hints: list[str], default_fit: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    name = str(item.get('name', '')).strip()
                    if not name:
                        continue
                    rows.append(
                        {
                            'name': self._repair_mojibake(name),
                            'fit_type': default_fit,
                            'reason': self._repair_mojibake(str(item.get('reason', 'llm_selected')).strip() or 'llm_selected'),
                            'confidence': float(item.get('confidence', 0.7)),
                        }
                    )
                elif isinstance(item, str) and item.strip():
                    rows.append({'name': self._repair_mojibake(item.strip()), 'fit_type': default_fit, 'reason': 'llm_selected', 'confidence': 0.7})
        if not rows:
            for hint in fallback_hints:
                if hint.strip():
                    rows.append({'name': hint.strip(), 'fit_type': default_fit, 'reason': 'provided hint', 'confidence': 0.7})
        dedup: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in rows:
            key = item['name'].casefold()
            if key in seen:
                continue
            seen.add(key)
            dedup.append(item)
        return dedup

    @staticmethod
    def _parse_json_content(content: Any) -> dict[str, Any]:
        text = str(content or '').strip()
        if not text:
            raise ValueError('empty_response_content')
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(parsed, dict):
                return parsed
            raise ValueError('parsed JSON is not an object')

        cleaned = PlannerLLMClient._strip_json_fence(text).strip()
        start = cleaned.find('{')
        end = cleaned.rfind('}')
        if start != -1 and end != -1 and end >= start:
            candidate = cleaned[start : end + 1]
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        raise ValueError('unable to parse valid JSON object from response')

    @staticmethod
    def _strip_json_fence(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith('```'):
            stripped = re.sub(r'^```(?:json)?\s*', '', stripped, flags=re.IGNORECASE)
            stripped = re.sub(r'\s*```$', '', stripped)
        return stripped

    @staticmethod
    def _repair_mojibake(text: str) -> str:
        if not text:
            return text

        # Only attempt repair for obvious mojibake patterns like "äº§å..." or "Ã...".
        suspicious_markers = ('Ã', 'Â', 'ä', 'å', 'æ', 'ç', 'è', 'é', 'ê', 'ï', 'ð')
        has_marker = any(m in text for m in suspicious_markers)
        latin_like_ratio = sum(1 for ch in text if ord(ch) < 256) / max(1, len(text))
        if not has_marker or latin_like_ratio < 0.6:
            return text

        try:
            repaired = text.encode('latin-1').decode('utf-8')
        except Exception:
            return text

        if not repaired or '\ufffd' in repaired:
            return text

        # If repaired string still has many C1/control chars, keep original.
        bad_control_count = len(re.findall(r'[\x00-\x1F\x7F-\x9F]', repaired))
        if bad_control_count > 0:
            return text

        return repaired


class _NullTrace:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False
