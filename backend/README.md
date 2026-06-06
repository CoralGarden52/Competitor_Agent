# Competitor Analysis Backend

Backend v1 for a Deer-Flow-inspired multi-agent competitor analysis workflow.

## Run

```bash
cd backend
uv run uvicorn app.main:app --reload --port 8010
```

## LLM Config (OPENAI-only)

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`

`backend` no longer reads `ARK_*` environment variables.

## Core Features

- Orchestrated stages: Plan -> Collect -> Normalize -> Analyze -> Draft -> QA -> Finalize
- Schema-first contracts: Evidence, Finding, Report, ReworkTicket
- Industry-extensible schema registry (core + domain extensions)
- QA-driven rework loop with structured tickets
- PostgreSQL-backed run/event/ticket persistence and replay APIs
- Optional schema evolution proposal generation

## PostgreSQL Runtime

Backend runtime now uses PostgreSQL only. Configure these variables in `backend/.env`:

- `POSTGRES_HOST`
- `POSTGRES_PORT`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_DB`

Sample migration from the legacy SQLite file:

```bash
cd backend
uv run python scripts/migrate_sqlite_to_postgres.py --sample-runs 5
```

## Collector Env Controls

- `COLLECTOR_MAX_URLS`: preview execution URL cap controlled by server `.env`.
- `COLLECTOR_PER_FIELD_LIMIT`: evidence cap per schema field (set `1` for experiment throttling).
- `/collector/preview` execution uses server-side `COLLECTOR_MAX_URLS` and `COLLECTOR_PER_FIELD_LIMIT` for throttling.
- `COLLECTOR_PREVIEW_AUTO_SAVE_ENABLED`: auto-save every `/collector/preview` response as local JSON (default `true`).
- `COLLECTOR_PREVIEW_SAVE_DIR`: auto-save directory (default `.data/collector_exports` under backend working directory).
- Auto-saved file name format: `collector_preview_result_YYYYMMDD_HHMMSS_<6hex>.json`.
- `SUBAGENT_ENABLED`: enable isolated collector deep-dive subagents for formal runs.
- `SUBAGENT_MAX_ROUNDS`, `SUBAGENT_MAX_TOOL_CALLS`, `SUBAGENT_MAX_TOKENS`, `SUBAGENT_TIMEOUT_SECONDS`: per-subagent hard budgets.
- `SUBAGENT_MAX_CONCURRENCY`, `SUBAGENT_MAX_TASKS_PER_COLLECT`: Collect-stage fan-out limits.

## Prompt-Only Preview

`POST /collector/preview` now accepts prompt-only request payload and returns handoff JSON.

```bash
curl -X POST "http://127.0.0.1:8010/collector/preview" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "通用AI智能体竞品分析",
    "industry_hint": "",
    "competitor_hints": []
  }'
```

Response includes:
- `candidates.direct` and `candidates.substitute` only (no `irrelevant`)
- `analysis_schema_plan` (dynamic schema with core fields enforced)
- `handoff_targets` for downstream deep-dive agents

Set `"deep_dive": true` in `/collector/preview` payloads to explicitly run subagents during preview. Preview keeps this disabled by default.

## PowerShell Commands (No curl)

If you are using Windows PowerShell, prefer `Invoke-RestMethod` (or `Invoke-WebRequest`) instead of `curl`.

### 1) Health check

```powershell
$base = "http://127.0.0.1:8010"
Invoke-RestMethod -Method Get -Uri "$base/health"
```

### 2) Prompt-only preview

```powershell
$base = "http://127.0.0.1:8010"
$payload = @{
  prompt = "通用AI智能体竞品分析"
  industry_hint = ""
  competitor_hints = @()
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Method Post `
  -Uri "$base/collector/preview" `
  -ContentType "application/json; charset=utf-8" `
  -Body $payload
```
