# Collector Tools Usage (Deer-Flow Style)

This project follows deer-flow's tool pattern:
- Keys come from environment variables (.env)
- Tool provider is chosen by config (use path), not hardcoded key in code
- Collector adapter calls provider tools and normalizes to Evidence

## 1) Do we need Tavily base_url?
No for the current deer-flow community Tavily tool.
- Tool path: `deerflow.community.tavily.tools:web_search_tool`
- Client creation uses `TavilyClient(api_key=...)`
- So `TAVILY_API_KEY` is required, `base_url` is not required by default.

## 2) Recommended provider mapping for this project
- Search:
  - Tavily: `TAVILY_API_KEY`
  - Serper: `SERPER_API_KEY`
  - Exa: `EXA_API_KEY`
  - Firecrawl: `FIRECRAWL_API_KEY`
  - InfoQuest: `INFOQUEST_API_KEY`
  - Baidu Qianfan search (planned): `QIANFAN_API_KEY`, `QIANFAN_SECRET_KEY`, optional `QIANFAN_BASE_URL`
  - Zhihu official API (planned): `ZHIHU_CLIENT_ID`, `ZHIHU_CLIENT_SECRET`, optional `ZHIHU_BASE_URL`
- Fetch:
  - Jina Reader: `JINA_API_KEY` (when required by runtime)

## 3) How to use (same idea as deer-flow)
1. Put keys in `Competitor_Analysis/.env`.
2. In provider code, read from env/config, never hardcode keys.
3. Keep provider interface unified:
   - `search(query, max_results) -> list[SearchHit]`
   - `fetch(url) -> content`
4. In collector pipeline:
   - planner -> multi-source search -> fetch -> normalize Evidence -> dedup/verify

## 4) Existing deer-flow references
- `config.example.yaml` shows provider switch via `use:` paths.
- `deerflow.community.tavily.tools` demonstrates API-key-based search/fetch.
- `deerflow.community.jina_ai.tools` demonstrates fetch tool integration.

## 5) Practical note
After editing `.env`, restart backend process to reload environment variables.
