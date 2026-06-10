# Competitor Analysis Backend

This service provides the backend workflow for the `Competitor_Analysis` project. It exposes FastAPI APIs for previewing collection plans, starting analysis runs, streaming progress, editing reports, and exporting questionnaires. The current runtime is built around PostgreSQL, Redis, FastAPI, and a multi-agent workflow orchestrated in `app/core/workflow.py`.

## Tech Stack

- FastAPI application entry: `app/main.py`
- Runtime service composition: `app/core/deps.py`
- Workflow orchestration: `app/core/workflow.py`
- Persistent storage: `app/core/storage.py`
- LLM / collector / agent modules: `app/agents`, `app/core/collector`, `harness/tools`
- Container startup: `Dockerfile` and `docker-compose.yml`

## Directory Overview

- `app/main.py`: creates the FastAPI app, mounts routers, enables CORS, and exposes `GET /healthz`
- `app/api/runs.py`: run creation, run stream, workspace, chat, report, questionnaire, and ops APIs
- `app/api/collector.py`: collector preview and provider health APIs
- `app/api/schema.py`: schema registry and masked runtime config APIs
- `app/core/workflow.py`: assembles planner, collector, analyst, writer, QA, cache, and subagent runtime
- `app/core/storage.py`: PostgreSQL bootstrap plus run/event/checkpoint/trace persistence
- `app/core/config.py`: loads `.env` from project root and `backend/.env`

## Docker Deployment

### 1. Prepare environment variables

From the `backend` directory:

```bash
cd Competitor_Analysis/backend
cp ".env example" .env
```


### 2. Start the full backend stack

```bash
cd Competitor_Analysis/backend
docker compose up -d --build
```


### 3. Verify the deployment

```bash
docker compose ps
curl http://127.0.0.1:8010/healthz
docker compose logs -f backend
```

Expected health response:

```json
{"status":"ok"}
```

### 4. Stop or rebuild

```bash
docker compose down
docker compose up -d --build
```


### 5. Persistent data

Docker volumes currently used:

- `backend_data`: backend runtime artifacts such as collector preview exports and questionnaire export files
- `backend_logs`: backend log files
- `postgres_data`: PostgreSQL data files
- `redis_data`: Redis append-only data

### 6. Server deployment notes

On a Linux server:

```bash
git clone <your-repo-url>
cd Competitor_Analysis/backend
cp ".env example" .env
docker compose up -d --build
```

Operational commands:

```bash
docker compose ps
docker compose logs -f backend
docker compose pull
docker compose up -d --build
```

## Local Development Startup

If you want to run without Docker, prepare PostgreSQL and Redis yourself, then:

```bash
cd Competitor_Analysis/backend
uv sync
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8010
```

Important local defaults from `app/core/config.py`:

- PostgreSQL host defaults to `localhost`
- PostgreSQL port defaults to `5433`
- Redis is disabled by default in code, but enabled in Docker Compose through env vars

## API Overview

### Health and runtime

- `GET /healthz`: service health check
- `GET /schema/runtime-config`: masked runtime config
- `GET /schema/registry`: active schema registry snapshot

### Collector preview

- `POST /collector/preview`: generate collection candidates, schema plan, and handoff targets from prompt input
- `GET /collector/providers/health`: provider health summary
- `GET /collector/llm/health`: collector LLM health summary

Example:

```bash
curl -X POST "http://127.0.0.1:8010/collector/preview" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "General AI agent competitor analysis",
    "industry_hint": "",
    "competitor_hints": [],
    "deep_dive": false
  }'
```

### Workflow runs

- `POST /runs`: create a run asynchronously
- `GET /runs`: list runs
- `GET /runs/{run_id}`: fetch run state
- `GET /runs/{run_id}/stream`: stream run events with SSE
- `GET /runs/{run_id}/workspace`: get frontend workspace payload
- `GET /runs/{run_id}/events`: query persisted events
- `GET /runs/{run_id}/replay`: replay a run timeline
- `POST /runs/{run_id}/ops/resume`: resume from latest checkpoint
- `POST /runs/{run_id}/ops/intervene`: manual intervention

### Plan confirmation and editing

- `GET /runs/{run_id}/plan-confirmation`
- `POST /runs/{run_id}/plan-confirmation/confirm`
- `POST /runs/{run_id}/plan-confirmation/supplement`
- `PATCH /runs/{run_id}/report`
- `GET /runs/{run_id}/report.md`

### Chat and questionnaire

- `POST /runs/{run_id}/chat`
- `GET /runs/{run_id}/chat`
- `GET /runs/{run_id}/chat/{turn_id}`
- `GET /runs/{run_id}/chat/{turn_id}/stream`
- `POST /runs/{run_id}/questionnaire`
- `PATCH /runs/{run_id}/questionnaire`
- `POST /runs/{run_id}/questionnaire/export/wenjuan`

## Backend Logic Implementation

### 1. App bootstrap

`app/main.py` creates the FastAPI app, registers:

- `runs_router`
- `schema_router`
- `collector_router`

It also enables permissive CORS and exposes `GET /healthz`.

### 2. Dependency assembly

`app/core/deps.py` builds a singleton `CompetitorWorkflowService` through `get_service()`:

- loads env config
- initializes Redis runtime and `WorkflowCache`
- creates `PostgresStore`
- selects `RedisChatStreamBroker` or `InMemoryChatStreamBroker`
- returns `CompetitorWorkflowService`

### 3. Storage bootstrap

`app/core/storage.py` does more than simple CRUD:

- creates the target PostgreSQL database automatically if it does not exist
- initializes all required tables on startup
- persists runs, events, stage handoffs, checkpoints, LLM traces, chat history, report revisions, and subagent runs
- provides replay and audit data used by the frontend workspace APIs

That means no separate migration container is required for the current backend bootstrap flow.

### 4. Workflow orchestration

The central runtime lives in `app/core/workflow.py`. `CompetitorWorkflowService` wires together:

- `PlannerLLMClient`
- `AgentLLMClient`
- `CollectorPipeline`
- `CollectorDeepDiveCoordinator`
- `OrchestratorAgent`
- `ManagerAgent`
- `CollectorAgent`
- `AnalystAgent`
- `WriterAgent`
- `QuestionnaireAgent`
- `QACriticAgent`
- `WorkflowLangGraphRuntime`

The main run lifecycle is:

1. Accept a `RunRequest`
2. Initialize `RunState`
3. Save the run and seed initial events
4. Execute the workflow asynchronously
5. Persist stage inputs, outputs, handoffs, events, and checkpoints
6. Expose progress through SSE and workspace APIs
7. Allow report editing, QA rework, questionnaire generation, and export

### 5. Stage model

The backend is organized around these stages:

- `plan`
- `confirm_plan`
- `collect`
- `normalize`
- `analyze`
- `draft`
- `qa`
- `finalize`

The documented summaries in `workflow.py` show the intended responsibilities:

- `plan`: identify analysis subjects and schema fields
- `confirm_plan`: wait for user confirmation or supplement
- `collect`: gather evidence and assign it to fields
- `analyze`: convert evidence into findings and competitor profiles
- `draft`: generate editable Markdown report
- `qa`: check completeness, references, and unknown gaps
- `finalize`: mark the run complete or failed

### 6. Collector path

`POST /collector/preview` calls `service.collector_preview(...)` with:

- `prompt`
- `industry_hint`
- `competitor_hints`
- `deep_dive`

The preview path is used to:

- infer candidate competitors
- build `analysis_schema_plan`
- return `handoff_targets`
- optionally trigger deep-dive collection
- optionally auto-save preview JSON under the configured preview directory

### 7. Run streaming and observability

The backend supports real-time frontend updates through SSE endpoints:

- `GET /runs/{run_id}/stream`
- `GET /runs/{run_id}/chat/{turn_id}/stream`

Progress data comes from a mix of:

- PostgreSQL persisted events
- Redis-backed run stream queues when Redis is enabled
- generated workspace payloads built from run state, traces, handoffs, and QA metadata

### 8. Report and questionnaire flow

After analysis:

- the writer agent generates Markdown report content
- the report can be patched through `PATCH /runs/{run_id}/report`
- the questionnaire agent can derive a questionnaire from the report
- the questionnaire can be edited and exported to Wenjuanxing when enabled

## Useful Commands

### PowerShell health check

```powershell
$base = "http://127.0.0.1:8010"
Invoke-RestMethod -Method Get -Uri "$base/healthz"
```

### PowerShell collector preview

```powershell
$base = "http://127.0.0.1:8010"
$payload = @{
  prompt = "General AI agent competitor analysis"
  industry_hint = ""
  competitor_hints = @()
  deep_dive = $false
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Method Post `
  -Uri "$base/collector/preview" `
  -ContentType "application/json; charset=utf-8" `
  -Body $payload
```

## Notes

- The current backend uses PostgreSQL as the primary runtime store.
- Redis is optional in code, but enabled by default in Docker Compose for cache and streaming support.
- The health endpoint is `/healthz`, not `/health`.
- The current README reflects the code under `Competitor_Analysis/backend` as of the present project state.
