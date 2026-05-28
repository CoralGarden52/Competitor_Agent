from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import concurrent.futures
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
            '{product} 核心功能',
            '{product} 官方文档 功能',
            '{product} 使用场景',
        ],
        'recommended_sources': ['官网', '文档', '产品页'],
        'priority': 1,
    },
    {
        'field_name': 'strengths',
        'query_templates': [
            '{product} 优势 评测',
            '{product} 对比 缺点',
            '{product} 为什么选择',
        ],
        'recommended_sources': ['评测', '分析', '社区'],
        'priority': 2,
    },
    {
        'field_name': 'weaknesses',
        'query_templates': [
            '{product} 劣势 局限',
            '{product} 对比 缺点',
            '{product} 缺点 评测',
        ],
        'recommended_sources': ['评测', '社区', '问题反馈'],
        'priority': 3,
    },
    {
        'field_name': 'pricing_model',
        'query_templates': [
            '{product} 价格 套餐',
            '{product} 企业版 计费',
            '{product} 免费版 价格',
        ],
        'recommended_sources': ['官网', '定价页', '文档'],
        'priority': 4,
    },
    {
        'field_name': 'user_feedback',
        'query_templates': [
            '{product} 评价',
            '{product} 点评',
            '{product} 体验',
            '{product} 反馈',
        ],
        'recommended_sources': ['知乎', '社区', '评测'],
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
            '只返回一个合法的 JSON 对象。'
            '不要输出 markdown 代码块。'
            '不要在 JSON 前后添加解释文本。'
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
            _ = self._chat_json('请返回严格 JSON。', '请返回 JSON：{"ok": true}')
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
        sys_prompt = '你是一位竞品发现助手。请返回严格 JSON。'
        user_prompt = (
            f'行业={industry}\n'
            f'用户提供的竞品={user_competitors}\n'
            '返回 JSON：{"competitors": ["名称1", "名称2", ...]}，最多 8 个。'
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
        sys_prompt = '你负责根据研究需求判断行业标签。请返回严格 JSON。'
        user_prompt = (
            f'研究需求={prompt}\n'
            '返回 JSON：{"industry":"简短行业标签"}'
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

    def discover_competitors_grouped(
        self,
        *,
        prompt: str,
        industry: str = '',
        competitor_hints: list[str],
        max_direct: int = 8,
        max_substitute: int = 6,
    ) -> dict[str, Any]:
        """基于搜索验证的竞品发现方法"""
        if not self.enabled():
            direct = [self._make_candidate(name=x, fit_type='direct', reason='provided hint') for x in competitor_hints if x.strip()]
            return {'competitors': {'direct': direct[:max_direct], 'substitute': []}, 'search_results': [], 'candidate_pool': competitor_hints}

        normalized_industry = str(industry or '').strip().lower()

        # 步骤1: 生成搜索 query
        search_queries = self._generate_search_queries(prompt, competitor_hints, industry=normalized_industry)
        if not search_queries:
            base_query = prompt.strip() or normalized_industry or '竞品分析'
            if normalized_industry and normalized_industry not in base_query:
                base_query = f'{normalized_industry} {base_query}'
            search_queries = [f'{base_query} 竞品', f'{base_query} 替代产品']

        # 步骤2: 执行搜索并收集结果
        search_results = self._search_and_summarize(search_queries)
        search_results = self._dedupe_search_results(search_results)
        candidate_pool = self._build_candidate_pool(
            prompt=prompt,
            industry=normalized_industry,
            competitor_hints=competitor_hints,
            search_results=search_results,
        )

        expansion_queries = self._build_expansion_queries(
            competitor_hints=competitor_hints,
            candidate_pool=candidate_pool,
        )
        if expansion_queries:
            expanded_results = self._search_and_summarize(expansion_queries)
            search_results = self._dedupe_search_results(search_results + expanded_results)
            candidate_pool = self._build_candidate_pool(
                prompt=prompt,
                industry=normalized_industry,
                competitor_hints=competitor_hints,
                search_results=search_results,
            )

        # 步骤3: 基于搜索结果发现竞品
        competitors = self._discover_from_search_results(
            prompt,
            normalized_industry,
            competitor_hints,
            search_results,
            candidate_pool,
            max_direct=max_direct, max_substitute=max_substitute
        )

        return {
            'competitors': competitors,
            'search_results': search_results,
            'candidate_pool': candidate_pool,
        }

    def _generate_search_queries(self, prompt: str, competitor_hints: list[str], *, industry: str = '') -> list[str]:
        """生成搜索竞品的 query"""
        generic_queries = self._build_generic_product_queries(prompt=prompt, industry=industry, competitor_hints=competitor_hints)
        if generic_queries:
            return generic_queries

        sys_prompt = """你是一位专业的竞品分析专家，擅长为竞品发现生成有效的搜索关键词。

任务要求：
1. 根据用户的研究需求，生成2-3个最有效的搜索关键词/短语
2. 搜索词应该能够找到相关的竞品信息
3. 搜索词应该精准定位目标竞品，避免歧义
4. 搜索词应该简洁，控制在10-20个字以内

输出格式：
{"search_queries": ["搜索词1", "搜索词2"]}

注意事项：
- 搜索词应该简洁明了，优先使用短词组
- 避免包含"同类竞品汇总"、"对比"等宽泛词汇
- 重点突出产品类别或核心功能
- 如果用户提到了具体产品或品牌，在搜索词中包含该产品名"""
        user_prompt = (
            f'用户研究需求：{prompt}\n'
            f'行业上下文：{industry}\n'
            f'已知的竞品线索：{competitor_hints}\n'
            '请生成2-3个最有效的搜索关键词来发现相关竞品。\n'
            '输出格式：{"search_queries": [...]}'
        )
        try:
            with self._trace_llm_call(name='planner.generate_search_queries', inputs={'prompt': prompt}):
                result = self._chat_json(sys_prompt, user_prompt)
            queries = result.get('search_queries', [])
            if isinstance(queries, list):
                return [str(q).strip() for q in queries if str(q).strip()][:4]
        except Exception as e:
            logger.warning(f"Failed to generate search queries: {e}")
        return []

    def _build_generic_product_queries(self, *, prompt: str, industry: str, competitor_hints: list[str]) -> list[str]:
        topic = self._extract_generic_topic(prompt, industry=industry)
        if not topic:
            return []

        seed = competitor_hints[0].strip() if competitor_hints else ''
        queries: list[str] = []
        seen: set[str] = set()

        def _add(query: str) -> None:
            cleaned = re.sub(r'\s+', ' ', query.strip())
            if not cleaned:
                return
            key = cleaned.casefold()
            if key in seen:
                return
            seen.add(key)
            queries.append(cleaned)

        _add(f'{topic} 官网')
        _add(f'{topic} 产品')
        _add(f'{topic} 竞品')
        if seed:
            _add(f'{seed} 替代品')
            _add(f'{seed} 竞品')

        return queries[:4]

    def _extract_generic_topic(self, prompt: str, *, industry: str) -> str:
        text = re.sub(r'\s+', ' ', str(prompt).strip())
        if not text:
            return ''

        generic_markers = (
            '软件', '工具', '平台', '助手', '系统', '服务', '应用',
            '会议', '办公', '协作', '编程', '开发',
        )
        normalized = text.casefold()
        if not any(marker in normalized for marker in generic_markers):
            return ''

        topic = re.sub(r'帮我做一个|帮我做一份|请分析|竞品分析|产品分析|市场分析', '', text, flags=re.IGNORECASE).strip()
        topic = re.sub(r'的竞品分析$|的产品分析$|的市场分析$|竞品分析$|产品分析$|市场分析$', '', topic).strip()
        topic = re.sub(r'的$', '', topic).strip()
        topic = re.sub(r'[，。,.!?！？]+$', '', topic).strip()
        if not topic:
            topic = text

        if industry and industry.casefold() not in topic.casefold():
            topic = f'{industry} {topic}'.strip()

        return topic[:60]

    def _search_and_summarize(self, queries: list[str]) -> list[dict[str, Any]]:
        """执行搜索并优先使用搜索结果本身的标题与摘要。"""
        from app.core.collector.pipeline import CollectorPipeline
        from app.core.collector.providers import TavilySearchProvider
        from app.core.collector.types import CollectorOutput
        from app.core.storage import SQLiteStore

        results: list[dict[str, Any]] = []
        tavily_provider = TavilySearchProvider(self.config)
        collector = None
        if not tavily_provider.health().available:
            try:
                collector = CollectorPipeline(self.config, SQLiteStore(self.config.sqlite_path_obj))
            except Exception as e:
                logger.warning(f"Failed to initialize collector: {e}")
                return results

        def _run_query(query: str) -> list[dict[str, Any]]:
            query_results: list[dict[str, Any]] = []
            try:
                if tavily_provider.health().available:
                    hits, _errors = tavily_provider.search(query, max_results=min(8, self.config.collector_max_results_per_query + 3))
                else:
                    output = CollectorOutput()
                    fallback_trace = []
                    hits = collector._run_search_phase(query=query, output=output, fallback_trace=fallback_trace) if collector is not None else []

                for hit in hits[: min(8, self.config.collector_max_results_per_query + 3)]:
                    summary = self._clean_search_summary(hit.snippet, hit.title)
                    if not summary:
                        continue
                    query_results.append({
                        'query': query,
                        'url': hit.url,
                        'title': hit.title,
                        'summary': summary,
                        'source_provider': getattr(hit, 'source_provider', ''),
                    })
            except Exception as e:
                logger.warning(f"Failed to search for query {query}: {e}")
            return query_results

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(max(1, len(queries[:4])), 4)) as executor:
            futures = [executor.submit(_run_query, query) for query in queries[:4]]
            for future in concurrent.futures.as_completed(futures):
                results.extend(future.result())
        return results

    def _build_expansion_queries(self, *, competitor_hints: list[str], candidate_pool: list[str]) -> list[str]:
        queries: list[str] = []
        seen: set[str] = set()

        def _add(query: str) -> None:
            cleaned = re.sub(r'\s+', ' ', query.strip())
            if not cleaned:
                return
            key = cleaned.casefold()
            if key in seen:
                return
            seen.add(key)
            queries.append(cleaned)

        seed_candidates = [name for name in competitor_hints if name.strip()]
        seed_candidates.extend(candidate_pool[:2])
        for name in seed_candidates[:2]:
            _add(f'{name} 替代品')
            _add(f'{name} 竞品')
        return queries[:4]

    def _dedupe_search_results(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            url = str(row.get('url', '')).strip()
            title = str(row.get('title', '')).strip()
            summary = str(row.get('summary', '')).strip()
            source_provider = str(row.get('source_provider', '')).strip()
            if not url:
                continue
            key = url.casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped.append({'query': row.get('query', ''), 'url': url, 'title': title, 'summary': summary, 'source_provider': source_provider})
        return deduped

    def _summarize_content(self, content: str, url: str) -> str:
        """总结网页内容，提取关键信息"""
        truncate_content = content[:2000] if len(content) > 2000 else content

        sys_prompt = """你是一位专业的竞品分析助手，擅长从网页内容中提取关键信息。

任务要求：
1. 阅读以下网页内容
2. 提取与竞品分析相关的关键信息
3. 特别关注：产品名称、功能特点、优劣势、定位、用户群体等
4. 用简洁的语言总结（不超过200字）

输出格式：
{"summary": "提取的关键信息摘要"}

注意事项：
- 只关注与竞品分析相关的信息
- 提取尽可能多的产品名称
- 保持信息的准确性"""
        user_prompt = (
            f'网页标题/URL：{url}\n'
            f'网页内容：\n{truncate_content}\n\n'
            '请提取关键竞品信息并用简洁的语言总结。'
        )
        try:
            with self._trace_llm_call(name='planner.summarize_content', inputs={'url': url}):
                result = self._chat_json(sys_prompt, user_prompt)
            summary = result.get('summary', '')
            if summary and len(summary) > 20:
                return summary
        except Exception as e:
            logger.warning(f"Failed to summarize content: {e}")

        # Fallback: 清理 HTML 标签，提取纯文本
        import re
        text = re.sub(r'<[^>]+>', ' ', truncate_content)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 50:
            return text[:200]
        return f"网页内容：{truncate_content[:150]}"

    def _discover_from_search_results(
        self,
        prompt: str,
        industry: str,
        competitor_hints: list[str],
        search_results: list[dict[str, Any]],
        candidate_pool: list[str],
        max_direct: int = 8,
        max_substitute: int = 6,
    ) -> dict[str, list[dict[str, Any]]]:
        """基于搜索结果和网页摘要发现竞品"""
        if not self.enabled():
            direct = [self._make_candidate(name=x, fit_type='direct', reason='provided hint') for x in competitor_hints if x.strip()]
            return {'direct': direct[:max_direct], 'substitute': []}

        # 构建搜索结果上下文
        search_context = ""
        if search_results:
            search_items = []
            for i, r in enumerate(search_results[:6], 1):
                search_items.append(f"[{i}] {r.get('title', 'N/A')}\n摘要: {r.get('summary', 'N/A')[:300]}")
            search_context = "\n\n搜索结果摘要：\n" + "\n\n".join(search_items)

        sys_prompt = """你是一位专业的竞品发现专家，擅长基于搜索结果识别相关竞品。

任务要求：
1. 基于用户的研究需求和搜索结果，识别相关的直接竞品和替代竞品
2. 只返回与用户需求真正相关的竞品
3. 如果搜索结果中没有找到相关竞品，返回空列表
4. 区分直接竞品（功能相似）和替代竞品（解决相同问题但方式不同）

输出格式：
{
  "direct": [{"name": "竞品名称", "reason": "为什么是直接竞品", "confidence": 0.8}],
  "substitute": [{"name": "竞品名称", "reason": "为什么是替代竞品", "confidence": 0.6}]
}

注意事项：
- 只返回真正相关的竞品，不要硬凑
- 如果搜索结果中没有找到相关信息，可以返回空列表
- confidence 表示你对这个判断的信心程度（0-1）
- 不要返回与用户需求明显无关的产品"""

        user_prompt = (
            f'用户研究需求：{prompt}\n'
            f'行业上下文：{industry}\n'
            f'已知的竞品线索：{competitor_hints}\n'
            f'候选池（只能从这里选择，不允许新增名称）：{candidate_pool}\n'
            f'{search_context}\n\n'
            '请基于以上搜索结果，识别相关的直接竞品和替代竞品。\n'
            '只返回真正相关的竞品，如果搜索结果中没有找到相关信息，可以返回空列表。'
        )

        try:
            with self._trace_llm_call(name='planner.discover_from_search', inputs={'prompt': prompt, 'search_result_count': len(search_results)}):
                result = self._chat_json(sys_prompt, user_prompt)
            self._record_step_status('discover_competitors_grouped')
        except Exception as e:
            logger.warning(f"Failed to discover from search results: {e}")
            self._record_step_status('discover_competitors_grouped')
            result = {}

        direct = self._clean_candidates(
            result.get('direct', []),
            fallback_hints=competitor_hints,
            default_fit='direct',
            allowed_names=candidate_pool,
        )
        substitute = self._clean_candidates(
            result.get('substitute', []),
            fallback_hints=[],
            default_fit='substitute',
            allowed_names=candidate_pool,
        )
        return {'direct': direct[:max_direct], 'substitute': substitute[:max_substitute]}

    def plan_dynamic_schema(
        self,
        *,
        prompt: str,
        industry: str,
        candidates: list[str],
        search_results: list[dict] = None,
    ) -> list[dict[str, Any]]:
        """基于真实搜索结果生成动态 schema"""
        if not self.enabled():
            return self._core_schema_plan_only()
        core_plan = self._core_schema_plan_only()
        extra_plan = self.plan_schema_extensions_from_prompt(
            prompt=prompt,
            core_schema_fields=CORE_DYNAMIC_FIELDS,
            candidate_names=candidates,
            search_results=search_results,
        )
        if extra_plan:
            self._record_step_status('plan_dynamic_schema')
            return self._normalize_dynamic_schema(core_plan + extra_plan)
        self._record_step_status('plan_dynamic_schema')
        return core_plan

    def _build_candidate_pool(
        self,
        *,
        prompt: str,
        industry: str,
        competitor_hints: list[str],
        search_results: list[dict[str, Any]],
    ) -> list[str]:
        llm_candidates = self._extract_candidates_with_llm(
            prompt=prompt,
            industry=industry,
            competitor_hints=competitor_hints,
            search_results=search_results,
        )
        if llm_candidates:
            merged: list[str] = []
            seen: set[str] = set()
            for hint in competitor_hints:
                cleaned_hint = self._clean_candidate_name(hint)
                key = self._normalize_candidate_key(cleaned_hint)
                if cleaned_hint and key not in seen:
                    seen.add(key)
                    merged.append(cleaned_hint)
            for name in llm_candidates:
                key = self._normalize_candidate_key(name)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(name)
            return merged[: max(self.config.planner_schema_max_candidates, 6)]

        score_map: dict[str, float] = {}
        display_map: dict[str, str] = {}
        doc_freq_map: dict[str, int] = {}
        blocked = {
            'ai', 'saas', '竞品', '替代产品', '对比', '产品', '平台', 'solution', 'solutions', 'software',
            'pricing', 'review', 'reviews', 'docs', 'official', '知乎', 'reddit', 'g2', 'capterra',
        }

        def _add(name: str, score: float, *, doc_seen: set[str] | None = None) -> None:
            cleaned = self._clean_candidate_name(name)
            if not cleaned:
                return
            key = self._normalize_candidate_key(cleaned)
            if key in blocked:
                return
            if industry and self._normalize_candidate_key(industry) == key:
                return
            if prompt and self._normalize_candidate_key(prompt) == key:
                return
            display_map.setdefault(key, cleaned)
            score_map[key] = score_map.get(key, 0) + score
            if doc_seen is not None and key not in doc_seen:
                doc_seen.add(key)
                doc_freq_map[key] = doc_freq_map.get(key, 0) + 1

        for hint in competitor_hints:
            _add(hint, 10.0)

        for item in search_results[:12]:
            title = str(item.get('title', '')).strip()
            summary = str(item.get('summary', '')).strip()
            url = str(item.get('url', '')).strip()
            page_weight = self._page_type_weight(title=title, url=url, summary=summary)
            doc_seen: set[str] = set()

            for part in self._extract_title_candidates(title):
                _add(part, 2.0 * page_weight, doc_seen=doc_seen)
            for token in self._extract_candidate_mentions(summary):
                _add(token, 3.0 * page_weight, doc_seen=doc_seen)
            for token in self._extract_list_candidates(summary):
                _add(token, 4.0 * page_weight, doc_seen=doc_seen)

        ranked_rows: list[tuple[str, float]] = []
        for key, raw_score in score_map.items():
            bonus = min(doc_freq_map.get(key, 0), 4) * 2.5
            if self._looks_like_brand_name(display_map.get(key, '')):
                bonus += 1.5
            ranked_rows.append((key, raw_score + bonus))

        ranked = sorted(
            ranked_rows,
            key=lambda item: (-item[1], -doc_freq_map.get(item[0], 0), len(display_map.get(item[0], '')), display_map.get(item[0], '').casefold()),
        )
        candidates = [display_map[key] for key, _score in ranked]
        return candidates[: max(self.config.planner_schema_max_candidates, 6)]

    def _extract_candidates_with_llm(
        self,
        *,
        prompt: str,
        industry: str,
        competitor_hints: list[str],
        search_results: list[dict[str, Any]],
    ) -> list[str]:
        if not self.enabled() or not search_results:
            return []

        page_payloads = self._build_llm_page_payloads(search_results[:8])
        sys_prompt = (
            '你是一位产品实体抽取助手。'
            '你的任务是从单个网页结果的内容中抽取“可独立作为竞品对象的具体产品名或品牌名”，用于竞品分析。'
            '只保留和当前竞品分析目标直接相关、处于同一竞争层级的产品。'
            '只返回严格 JSON。'
        )

        def _extract_one(index: int, page_payload: dict[str, str]) -> list[str]:
            user_prompt = (
                f'我们正在做“{prompt}”的竞品分析。\n'
                f'行业上下文：{industry}\n'
                f'已知线索：{competitor_hints}\n'
                f'这是第 {index} 个网页结果，请从其中抽取真实出现过的、可以独立作为竞品对象的产品名或品牌名。\n'
                '只保留和本次竞品分析目标直接相关的候选。\n'
                '如果网页内容主要在讲某个平台的扩展功能、附属模块、硬件生态、外围品牌或无关工具，不要把这些词返回为候选。\n'
                '只保留和当前目标同一层级、可直接拿来做竞品对比的产品或品牌。\n'
                '不要返回站点名、栏目名、普通短语、功能短语、观点句。\n'
                '不要返回某个平台内部的功能模块、子页面、子能力名称。\n'
                '例如：文档、日历、表单、表格、幻灯片、知识库、待办、邮箱，这些通常应当排除，除非该词本身明确是独立产品名。\n'
                '如果网页提到的是会议软件竞品，就优先保留会议软件本身，不要返回与会议无直接竞争关系的协作模块、外设品牌、社交工具或周边服务。\n'
                '如果正文里同时出现“平台名”和“平台下的模块名”，只保留平台名。\n'
                '如果没有明确产品名，可以返回空数组。\n'
                '返回 JSON: {"candidate_names":["产品A","产品B"]}\n\n'
                f'标题: {page_payload.get("title", "")}\n'
                f'URL: {page_payload.get("url", "")}\n'
                f'网页内容摘要: {page_payload.get("summary", "")}\n'
                f'网页正文节选: {page_payload.get("content", "")}'
            )
            try:
                with self._trace_llm_call(
                    name='planner.extract_candidates_with_llm.page',
                    inputs={'prompt': prompt, 'page_index': index, 'url': page_payload.get('url', '')},
                ):
                    result = self._chat_json(sys_prompt, user_prompt)
            except Exception as exc:
                logger.warning('Failed to extract candidate names with llm for page %s: %s', index, exc)
                return []

            raw_names = result.get('candidate_names', [])
            if not isinstance(raw_names, list):
                return []

            cleaned: list[str] = []
            seen: set[str] = set()
            for item in raw_names:
                name = self._clean_candidate_name(str(item).strip())
                key = self._normalize_candidate_key(name)
                if not name or key in seen:
                    continue
                seen.add(key)
                cleaned.append(name)
            return cleaned

        vote_map: dict[str, int] = {}
        display_map: dict[str, str] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(page_payloads), 4)) as executor:
            futures = [
                executor.submit(_extract_one, index, page_payload)
                for index, page_payload in enumerate(page_payloads, 1)
            ]
            for future in concurrent.futures.as_completed(futures):
                for name in future.result():
                    key = self._normalize_candidate_key(name)
                    display_map.setdefault(key, name)
                    vote_map[key] = vote_map.get(key, 0) + 1

        if not vote_map:
            return []

        ranked = sorted(
            vote_map.items(),
            key=lambda item: (-item[1], len(display_map.get(item[0], '')), display_map.get(item[0], '').casefold()),
        )
        return [display_map[key] for key, _votes in ranked[: max(self.config.planner_schema_max_candidates, 6)]]

    def _build_llm_page_payloads(self, search_results: list[dict[str, Any]]) -> list[dict[str, str]]:
        from app.core.collector.providers import TavilyExtractProvider

        provider = TavilyExtractProvider(self.config)

        def _fetch_one(row: dict[str, Any]) -> dict[str, str]:
            title = str(row.get('title', '')).strip()
            url = str(row.get('url', '')).strip()
            summary = str(row.get('summary', '')).strip()[:600]
            content = ''
            if provider.health().available and url:
                try:
                    fetched, _errors = provider.fetch(url)
                    if fetched:
                        content = re.sub(r'\s+', ' ', str(fetched).strip())[:1800]
                except Exception as exc:
                    logger.warning('Failed to fetch page content for llm extraction %s: %s', url, exc)
            return {
                'title': title,
                'url': url,
                'summary': summary,
                'content': content,
            }

        payloads: list[dict[str, str]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(search_results), 4)) as executor:
            futures = [executor.submit(_fetch_one, row) for row in search_results]
            for future in concurrent.futures.as_completed(futures):
                payload = future.result()
                if payload.get('title') or payload.get('summary') or payload.get('content'):
                    payloads.append(payload)
        return payloads

    def _extract_title_candidates(self, title: str) -> list[str]:
        if self._is_article_like_title(title):
            return []
        rows: list[str] = []
        for part in re.split(r'[-:|｜/,，()\[\]]', title):
            text = self._repair_mojibake(part.strip())
            if text:
                rows.append(text)
        rows.extend(self._extract_candidate_mentions(title))
        return rows

    @staticmethod
    def _is_article_like_title(title: str) -> bool:
        lowered = str(title or '').casefold()
        article_markers = (
            '竞品分析', '测评', '报告', '排行榜', '推荐', '盘点', '对比', '比拼',
            '你用对了吗', '秘密武器', '哪家强', '有哪些', '怎么选', '人人都是产品经理',
            '牛客网', '36氪', '21财经',
        )
        return any(marker.casefold() in lowered for marker in article_markers)

    def _extract_candidate_mentions(self, text: str) -> list[str]:
        snippet = self._repair_mojibake(str(text).strip())
        if not snippet:
            return []

        matches: list[str] = []
        patterns = (
            r'([A-Z][A-Za-z0-9 .&+\-]{1,30})(?=\s*(?:是|提供|支持|推出|作为|帮助|,|，|。|；|;|\n))',
            r'([\u4e00-\u9fffA-Za-z0-9·]{2,16})(?=\s*(?:是|提供|支持|推出|作为|帮助|,|，|。|；|;|\n))',
        )
        for pattern in patterns:
            matches.extend(re.findall(pattern, snippet))

        cleaned: list[str] = []
        seen: set[str] = set()
        for item in matches:
            value = self._clean_candidate_name(item)
            key = self._normalize_candidate_key(value)
            if not value or key in seen:
                continue
            seen.add(key)
            cleaned.append(value)
        return cleaned

    def _extract_list_candidates(self, text: str) -> list[str]:
        snippet = self._repair_mojibake(str(text).strip())
        if not snippet:
            return []

        matches = re.findall(r'(?:[一二三四五六七八九十0-9]+[、.．]\s*)([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9· ]{1,20})', snippet)
        matches.extend(re.findall(r'(?:包括|包含|如|例如)[:：]?\s*([\u4e00-\u9fffA-Za-z0-9·、，, /]{4,120})', snippet))

        candidates: list[str] = []
        seen: set[str] = set()
        for item in matches:
            parts = re.split(r'[、，,/]|以及|和|及', item)
            for part in parts:
                value = self._clean_candidate_name(part)
                key = self._normalize_candidate_key(value)
                if not value or key in seen:
                    continue
                seen.add(key)
                candidates.append(value)
        return candidates

    @staticmethod
    def _page_type_weight(*, title: str, url: str, summary: str) -> float:
        text = f'{title} {url} {summary}'.casefold()
        if any(marker in text for marker in ('alternatives', 'competitors', '替代', '竞品', '对比', '有哪些', 'best', 'top')):
            return 1.4
        if any(marker in text for marker in ('官网', 'official', 'pricing', 'feishu.cn', 'zoom.us', 'teams.microsoft.com')):
            return 1.2
        if any(marker in text for marker in ('报告', '资讯', '新闻', 'blog')):
            return 0.85
        return 1.0

    @staticmethod
    def _looks_like_brand_name(text: str) -> bool:
        if not text:
            return False
        if re.search(r'[A-Z]', text):
            return True
        return bool(re.search(r'(会议|文档|飞书|钉钉|腾讯|华为|石墨|语雀|Zoom|Slack|Notion|Trello|Asana)', text, flags=re.IGNORECASE))

    def _clean_candidate_name(self, value: str) -> str:
        text = self._repair_mojibake(str(value).strip())
        text = re.sub(r'\s+', ' ', text)
        text = re.sub(r'^[\W_]+|[\W_]+$', '', text)
        if len(text) < 2 or len(text) > 40:
            return ''
        lowered = text.casefold()
        exact_stopwords = {
            '人人都', '无论', '此外', '这种情况下', '平台简介', '疫情期间', '资源池大',
            '产品经理', '秘密武器', '在线会议', '远程视频', '企业都在用', '多特手游',
            '你用对了吗', '协同办公类saas产品',
        }
        leading_phrase_markers = (
            '没有', '为了', '对于', '由于', '如果', '随着', '通过', '使用', '支持',
            '可以', '能够', '以及', '更加', '主要', '界面', '安全', '痛点',
        )
        generic_text_markers = (
            '排行榜', '哪些好用', '分享', '测评', '一目了然', '怎么选', '指南', '报告',
            '优质', '主流', '六大', '十大', '最佳', '推荐', '合集', '盘点',
            '会议软件', '办公软件', '协作办公', '线上会议', '远程会议', '视频会议',
            '产品', '竞品', '替代品', '解决方案', '通信世界网',
        )
        generic_markers = (
            'official', 'docs', 'documentation', 'pricing', 'review', 'reviews', 'blog', 'news',
            '官网', '价格', '评测', '下载', '登录',
        )
        if text in exact_stopwords:
            return ''
        if any(text.startswith(marker) for marker in leading_phrase_markers):
            return ''
        if any(marker in text for marker in generic_text_markers):
            return ''
        if any(marker in lowered for marker in generic_markers):
            return ''
        if text.endswith(('网', '资讯', '财经', '产品经理')):
            return ''
        if re.search(r'[\u4e00-\u9fff]{8,}', text):
            return ''
        if re.fullmatch(r'[a-z0-9-]{2,}', lowered) and len(text) <= 5:
            return ''
        return text

    @staticmethod
    def _clean_search_summary(snippet: str, title: str) -> str:
        text = re.sub(r'\s+', ' ', str(snippet or '').strip())
        if len(text) >= 40:
            return text[:500]
        fallback = re.sub(r'\s+', ' ', str(title or '').strip())
        return fallback[:200]

    def plan_schema_extensions_from_prompt(
        self,
        *,
        prompt: str,
        core_schema_fields: list[str],
        candidate_names: list[str],
        search_results: list[dict] = None,
    ) -> list[dict[str, Any]]:
        """基于真实搜索结果生成额外的 schema 字段"""
        if not self.enabled():
            return []
        
        limited_candidates = [str(x).strip() for x in candidate_names if str(x).strip()][: self.config.planner_schema_max_candidates]
        
        # 构建搜索结果摘要
        search_context = ""
        if search_results:
            search_items = []
            for i, r in enumerate(search_results[:6], 1):
                search_items.append(f"[{i}] {r.get('title', 'N/A')}\n摘要: {r.get('summary', 'N/A')[:300]}")
            search_context = "\n\n真实搜索结果摘要：\n" + "\n\n".join(search_items)
        
        sys_prompt = (
            '你是一位专业的竞品分析规划专家，擅长基于真实的搜索结果设计分析维度。'
            '你的任务是基于用户需求和真实搜索结果，设计高价值的增量分析字段。'
            '只返回严格的 JSON。\n'
            '优先选择可以从公共来源研究的具体、便于取证的字段。'
            '不要返回通用填充字段、核心字段的同义词或过于模糊无法搜索的字段。'
        )
        
        user_prompt = (
            f'用户研究需求：{prompt}\n'
            f'核心字段（已有）：{core_schema_fields}\n'
            f'竞品列表：{limited_candidates}\n'
            f'{search_context}\n'
            '任务规则:\n'
            '1. 首先分析搜索结果摘要，了解这些竞品的真实情况。\n'
            '2. 只添加 core_schema_fields 之外的增量字段。\n'
            '3. 字段必须有助于区分此请求中列出的竞品。\n'
            '4. 相关时优先考虑部署模型、目标客户、生态系统、工作流程深度、合规支持、'
            '集成覆盖、定制化、分析深度、协作支持、入职复杂性、AI能力、'
            '安全控制或本地化支持等维度。\n'
            '5. 不包括优势、劣势、定价模型、用户反馈、功能树、功能、定价、评论、概述、'
            '摘要或其他通用别名。\n'
            '6. 每个 query_template 必须足够具体以进行网络搜索，并应提及要调查的实际角度。\n'
            '7. 使用 snake_case 格式的 field_name 值。\n'
            '好例子:\n'
            '{"field_name":"deployment_model","query_templates":["{product} 部署方式 私有化","{product} 公有云 本地部署"],'
            '"recommended_sources":["官网","文档","安全合规"],"priority":1}\n'
            '坏例子:\n'
            '{"field_name":"feature","query_templates":["{product} 功能"],"recommended_sources":["官网"],"priority":1}\n'
            '返回 JSON: {"extra_schema_fields":[{"field_name":"","query_templates":["{product} ..."],'
            '"recommended_sources":["官网"],"priority":1}]}. '
            '0-6 个字段；不要重复任何核心字段名称。'
        )
        
        try:
            with self._trace_llm_call(
                name='planner.plan_schema_extensions',
                inputs={'prompt': prompt, 'core_schema_fields': core_schema_fields, 'candidate_names': limited_candidates, 'search_result_count': len(search_results) if search_results else 0},
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
        sys_prompt = '你负责设计竞品分析 schema 方案。请返回严格 JSON。'
        user_prompt = (
            f'行业={industry}\n'
            f'目标产品={target_product}\n'
            f'竞品列表={competitors}\n'
            '返回 JSON：{"schema_plan":[{"field_name":"", "query_templates":["{product} ..."], "recommended_sources":[""], "priority":1}]}。'
            '需要 6-10 个字段。'
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
            q = self._normalize_query_templates(field_name=field_name, templates=item.get('query_templates', []))
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
            default_q = self._default_query_templates_for_field(field_name)
            default_sources = ['community', 'review'] if field_name == 'user_feedback' else ['official', 'public_web']
            cleaned.append(
                {'field_name': field_name, 'query_templates': default_q, 'recommended_sources': default_sources, 'priority': len(cleaned) + 1}
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
            q = self._normalize_query_templates(field_name=field_name, templates=item.get('query_templates', []))
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

    def _normalize_query_templates(self, *, field_name: str, templates: Any) -> list[str]:
        raw_templates = templates if isinstance(templates, list) else []
        cleaned: list[str] = []
        seen: set[str] = set()
        for template in raw_templates:
            text = self._repair_mojibake(str(template).strip())
            if not text:
                continue
            if '{product}' not in text:
                text = f'{{product}} {text}'
            if field_name == 'pricing_model':
                text = self._ensure_official_prefix_for_pricing_query(text)
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(text)

        if len(cleaned) < 2 or all(self._is_placeholder_query(field_name, item) for item in cleaned):
            for fallback in self._default_query_templates_for_field(field_name):
                key = fallback.casefold()
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append(fallback)
        return cleaned[:4]

    @staticmethod
    def _is_placeholder_query(field_name: str, query: str) -> bool:
        normalized = re.sub(r'\s+', ' ', query.strip().casefold())
        return normalized in {
            f'{{product}} {field_name}',
            f'{{product}} {field_name.replace("_", " ")}',
        }

    @staticmethod
    def _default_query_templates_for_field(field_name: str) -> list[str]:
        defaults = {
            'feature_tree': ['{product} 核心功能', '{product} 官方文档 功能'],
            'strengths': ['{product} 优势 评测', '{product} 对比 优势'],
            'weaknesses': ['{product} 劣势 局限', '{product} 问题 吐槽'],
            'pricing_model': ['{product} 官网 价格 套餐', '{product} 官网 企业版 计费'],
            'user_feedback': ['{product} 评价', '{product} 点评', '{product} 体验', '{product} 反馈'],
        }
        return defaults.get(field_name, [f'{{product}} {field_name}', f'{{product}} {field_name} 官网'])

    @staticmethod
    def _ensure_official_prefix_for_pricing_query(template: str) -> str:
        text = re.sub(r'\s+', ' ', str(template or '').strip())
        if not text:
            return text
        lowered = text.casefold()
        pricing_markers = ('价格', '套餐', '计费', 'pricing', 'price', 'plans', 'billing')
        official_markers = ('官网', 'official')
        if not any(marker in lowered for marker in pricing_markers):
            return text
        if any(marker in lowered for marker in official_markers):
            return text
        if '{product}' in text:
            return text.replace('{product}', '{product} 官网', 1)
        return f'官网 {text}'

    @staticmethod
    def _make_candidate(*, name: str, fit_type: str, reason: str) -> dict[str, Any]:
        return {'name': name.strip(), 'fit_type': fit_type, 'reason': reason, 'confidence': 0.7}

    def _clean_candidates(
        self,
        raw: Any,
        *,
        fallback_hints: list[str],
        default_fit: str,
        allowed_names: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        allowed_map = None
        if allowed_names is not None:
            allowed_map = {self._normalize_candidate_key(name): name for name in allowed_names if self._normalize_candidate_key(name)}
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    name = str(item.get('name', '')).strip()
                    if not name:
                        continue
                    normalized_name = self._normalize_candidate_key(name)
                    if allowed_map is not None and normalized_name not in allowed_map:
                        continue
                    rows.append(
                        {
                            'name': (allowed_map or {}).get(normalized_name, self._repair_mojibake(name)),
                            'fit_type': default_fit,
                            'reason': self._repair_mojibake(str(item.get('reason', 'llm_selected')).strip() or 'llm_selected'),
                            'confidence': float(item.get('confidence', 0.7)),
                        }
                    )
                elif isinstance(item, str) and item.strip():
                    normalized_name = self._normalize_candidate_key(item)
                    if allowed_map is not None and normalized_name not in allowed_map:
                        continue
                    rows.append(
                        {
                            'name': (allowed_map or {}).get(normalized_name, self._repair_mojibake(item.strip())),
                            'fit_type': default_fit,
                            'reason': 'llm_selected',
                            'confidence': 0.7,
                        }
                    )
        if not rows:
            for hint in fallback_hints:
                normalized_name = self._normalize_candidate_key(hint)
                if hint.strip() and (allowed_map is None or normalized_name in allowed_map):
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
    def _normalize_candidate_key(value: str) -> str:
        return re.sub(r'\s+', ' ', str(value).strip()).casefold()

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
