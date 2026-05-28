from __future__ import annotations

import json
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='', env_file='.env', extra='ignore')

    sqlite_path: str = '.data/competitor_analysis.db'
    max_rework_iterations: int = Field(default=2, ge=1, le=5)
    enable_schema_evolution: bool = True

    # Runtime / OpenAI-compatible config
    openai_api_key: str = ''
    openai_base_url: str = ''
    openai_model: str = 'gpt-4.1-mini'

    # Collector providers
    tavily_api_key: str = ''
    serper_api_key: str = ''
    exa_api_key: str = ''
    firecrawl_api_key: str = ''
    jina_api_key: str = ''
    jina_user_agent: str = ''
    baidu_search_api_key: str = ''
    baidu_search_endpoint: str = 'https://qianfan.baidubce.com/v2/ai_search/web_search'
    zhihu_client_id: str = ''
    zhihu_client_secret: str = ''
    zhihu_base_url: str = 'https://api.zhihu.com'
    zhihu_search_access_secret: str = ''
    zhihu_search_endpoint: str = 'https://developer.zhihu.com/api/v1/content/zhihu_search'
    bing_api_key: str = ''
    bing_endpoint: str = 'https://api.bing.microsoft.com/v7.0/search'
    request_timeout_seconds: int = Field(default=20, ge=3, le=600)
    planner_llm_retry_count: int = Field(default=2, ge=0, le=6)
    planner_llm_retry_backoff_ms: int = Field(default=800, ge=50, le=10000)
    planner_llm_retry_max_backoff_ms: int = Field(default=4000, ge=100, le=30000)
    planner_schema_max_candidates: int = Field(default=8, ge=1, le=20)
    max_search_results: int = Field(default=8, ge=1, le=20)
    collector_timeout_sec: int = Field(default=12, ge=3, le=60)
    collector_max_results_per_query: int = Field(default=5, ge=1, le=20)
    collector_provider_timeout_sec: int = Field(default=12, ge=3, le=600)
    collector_provider_retry: int = Field(default=1, ge=0, le=3)
    collector_search_order: str = 'tavily,qianfan,serper,exa,firecrawl_search,zhihu_official'
    collector_search_order_strict: bool = True
    collector_fetch_order: str = 'jina,firecrawl_fetch,tavily_extract'
    collector_cache_enabled: bool = True
    collector_cache_ttl_days: int = Field(default=30, ge=1, le=365)
    collector_max_urls: int = Field(default=10, ge=1, le=100)
    collector_per_field_limit: int = Field(default=3, ge=1, le=20)
    collector_preview_auto_save_enabled: bool = True
    collector_preview_save_dir: str = '.data/collector_exports'
    tracing_mode: str = 'relaxed'
    agent_llm_retry_count: int = Field(default=2, ge=0, le=6)
    agent_llm_retry_backoff_ms: int = Field(default=400, ge=50, le=10000)
    agent_llm_retry_max_backoff_ms: int = Field(default=2000, ge=100, le=30000)
    agent_llm_fallback_enabled: bool = True
    agent_llm_fallback_on_validation_error: bool = True
    analyze_llm_max_workers: int = Field(default=6, ge=1, le=32)
    report_truncation_enabled: bool = False
    report_truncation_limits_json: str = ''

    @property
    def sqlite_path_obj(self) -> Path:
        return Path(self.sqlite_path)

    def has_openai_config(self) -> bool:
        return bool(self.openai_api_key and self.openai_base_url and self.openai_model)

    @property
    def qianfan_api_key(self) -> str:
        return self.baidu_search_api_key

    @property
    def qianfan_search_endpoint(self) -> str:
        return self.baidu_search_endpoint

    @property
    def collector_search_order_list(self) -> list[str]:
        return [item.strip() for item in self.collector_search_order.split(',') if item.strip()]

    @property
    def collector_fetch_order_list(self) -> list[str]:
        return [item.strip() for item in self.collector_fetch_order.split(',') if item.strip()]

    @property
    def report_truncation_limits(self) -> dict[str, int]:
        defaults = {
            'matrix_cell': 110,
            'comparison_overview': 90,
            'opportunity': 120,
            'strength_weakness': 160,
            'matrix_highlight': 50,
        }
        raw = self.report_truncation_limits_json.strip()
        if not raw:
            return defaults
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return defaults
        if not isinstance(data, dict):
            return defaults
        merged = dict(defaults)
        for key, value in data.items():
            if key not in defaults:
                continue
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                merged[key] = parsed
        return merged

    def masked_runtime_config(self) -> dict[str, object]:
        masked_key = ''
        if self.openai_api_key:
            if len(self.openai_api_key) <= 8:
                masked_key = '*' * len(self.openai_api_key)
            else:
                masked_key = f'{self.openai_api_key[:8]}...{self.openai_api_key[-4:]}'
        return {
            'openai_model': self.openai_model,
            'openai_base_url': self.openai_base_url,
            'openai_api_key_masked': masked_key,
            'openai_config_ready': self.has_openai_config(),
            'report_truncation_enabled': self.report_truncation_enabled,
            'report_truncation_limits': self.report_truncation_limits,
        }


_CONFIG = AppConfig()


def get_config() -> AppConfig:
    return _CONFIG
