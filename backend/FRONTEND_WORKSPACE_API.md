# Frontend Display API Guide

This document is for coding agents and frontend engineers who need to build the
workspace UI, status pages, replay views, and observability panels against the
current backend.

It covers all display-oriented backend interfaces, what each one is for, which
fields should be treated as canonical for UI, and how to combine them for
runtime, replay, and demo views.

## Scope

This backend currently exposes display-relevant interfaces in three groups:

- Run lifecycle and workspace display
- Collector preview and system readiness
- Schema and runtime configuration display

The most important display payload is:

- `GET /runs/{run_id}/workspace`

That endpoint should be treated as the canonical UI snapshot for the main
workspace page.

## Endpoint Index

### Run lifecycle

- `POST /runs`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/workspace`
- `GET /runs/{run_id}/events`
- `GET /runs/{run_id}/stream`
- `GET /runs/{run_id}/replay`
- `GET /runs/{run_id}/nodes/{node_name}`
- `GET /runs/{run_id}/logs/export`
- `POST /runs/{run_id}/ops/resume`
- `POST /runs/{run_id}/ops/intervene`

### Preview and health

- `POST /collector/preview`
- `GET /collector/providers/health`
- `GET /collector/llm/health`

### Schema and runtime

- `GET /schema/registry`
- `GET /schema/runtime-config`

## Recommended Frontend Page Model

The backend supports these frontend screens cleanly:

1. Run launcher
2. Preview / plan preview
3. Run list / recent history
4. Live workspace
5. Agent detail / drill-down
6. Replay / timeline review
7. Logs / observability page
8. Schema registry / runtime readiness page

If you are building a single-page workspace, the recommended data flow is:

1. Optional pre-run preview via `POST /collector/preview`
2. Start run via `POST /runs`
3. Fetch initial snapshot via `GET /runs/{run_id}/workspace`
4. Subscribe to `GET /runs/{run_id}/stream`
5. Use `GET /runs/{run_id}/events` as SSE fallback or catch-up
6. Use `GET /runs/{run_id}/nodes/{node_name}` for focused drill-down
7. Use `GET /runs/{run_id}/logs/export` for export/download, not for primary UI

## 1. Start Run

### `POST /runs`

Purpose:

- Start an async run from the frontend

Request:

```json
{
  "industry": "saas",
  "competitors": ["OpenAI", "Anthropic"],
  "user_prompt": "请分析 AI 智能体竞品",
  "language": "zh-CN",
  "timeframe": "last_12_months"
}
```

Response:

- Standard `RunResponse`
- `summary` is the run header summary
- `state.run_id` is the canonical run identifier

Frontend usage:

- Use `state.run_id` as the stable key
- Immediately call `GET /runs/{run_id}/workspace`
- Then open `EventSource(/runs/{run_id}/stream)`

Do not:

- wait only on `GET /runs/{run_id}` to build the main UI

## 2. Run List

### `GET /runs`

Purpose:

- Populate sidebar/history/recent runs

Response shape:

Array of `RunSummary`

Important fields:

- `run_id`
- `industry`
- `status`
- `competitor_count`
- `created_at`
- `updated_at`

Frontend usage:

- Use for history cards and run switcher
- Prefer `updated_at` ordering visually
- Clicking one run should load `GET /runs/{run_id}/workspace`

## 3. Basic Run Status

### `GET /runs/{run_id}`

Purpose:

- Lightweight run state lookup
- Polling fallback when SSE is unavailable

Important fields:

- `summary.status`
- `state.status`
- `state.run_id`
- `state.attempt`

Frontend usage:

- Use only for quick fallback state checks
- Do not use this as the main display payload for the workspace

## 4. Main Workspace Snapshot

### `GET /runs/{run_id}/workspace`

Purpose:

- Canonical full snapshot for the main frontend workspace

Top-level keys:

- `summary`
- `request`
- `run`
- `workflow`
- `qa`
- `report`
- `artifacts`
- `observability`

This is the primary interface for display.

## 4.1 `summary`

Purpose:

- Header metadata for the run

Fields:

- `run_id`
- `industry`
- `status`
- `competitor_count`
- `created_at`
- `updated_at`

Recommended UI:

- Workspace top bar
- History detail summary

## 4.2 `request`

Purpose:

- Show the normalized request used to start the run

Fields:

- `industry`
- `user_prompt`
- `competitors`
- `language`
- `timeframe`

Recommended UI:

- Request detail panel
- “Original task” drawer or debug section

## 4.3 `run`

Purpose:

- Current run-level execution summary

Fields:

- `run_id`
- `status`
- `industry`
- `planned_competitors`
- `schema_fields`
- `evidence_count`
- `finding_count`
- `competitor_count`

Recommended UI:

- KPI cards
- Workspace hero section
- Status pill + top-line metrics

## 4.4 `workflow`

Purpose:

- Show orchestration structure and stage-to-stage flow

Fields:

- `dag`
- `timeline`
- `agent_stages`
- `agent_workflows`
- `agent_handoffs`
- `handoffs`

### `workflow.dag`

Fields:

- `nodes`
- `edges`

Recommended UI:

- Main DAG board
- High-level stage sequence visualization

### `workflow.timeline`

This is built from agent run traces and contains stage execution records.

Useful fields in items:

- `trace_id`
- `node_name`
- `attempt`
- `status`
- `started_at`
- `ended_at`
- `duration_ms`
- `error_text`

Recommended UI:

- Chronological stage timeline
- Stage duration table
- Replay header rail

### `workflow.agent_stages`

This is the primary source for stage summary cards.

Fields:

- `stage`
- `agent`
- `status`
- `duration_ms`
- `summary`
- `handoff_type`
- `handoff_summary`

Recommended UI:

- Left/right stage list
- Progress tracker
- Current-stage summary

### `workflow.agent_workflows`

Purpose:

- Show internal step graph for each stage

Fields:

- `nodes`
- `edges`

Recommended UI:

- Per-stage mini subflow diagram
- Internal agent step chip row

### `workflow.agent_handoffs`

Purpose:

- Canonical schema-flow display between agents

Each item:

```json
{
  "stage": "analyze",
  "agent_name": "Analyst Agent",
  "status": "completed",
  "input_schema": {
    "schema_name": "CollectHandoff",
    "payload": {},
    "created_at": "..."
  },
  "output_schema": {
    "schema_name": "AnalyzeHandoff",
    "payload": {},
    "created_at": "..."
  },
  "handoff_summary": "...",
  "handoff_highlights": ["..."]
}
```

Recommended UI:

- One schema-flow card per stage
- Show `input_schema.schema_name -> output_schema.schema_name`
- Show compact summary first
- Keep raw payload JSON collapsed by default

### `workflow.handoffs`

Purpose:

- Lower-level list of all recorded downstream handoffs

Fields:

- `stage`
- `attempt`
- `handoff_type`
- `created_at`
- `summary`
- `highlights`
- `payload`

Recommended UI:

- Handoff history drawer
- Debug/replay detail panel

Frontend note:

- Prefer `workflow.agent_handoffs` for the main schema-flow UI
- Prefer `workflow.handoffs` for full history/debug

## 4.5 `qa`

Purpose:

- Show QA state, issues, and recollect guidance

Fields:

- `passed`
- `target_agent`
- `issue_count`
- `issues`
- `collect_items`

### `qa.issues`

Each issue contains:

- `code`
- `message`
- `stage`

### `qa.collect_items`

Each item contains:

- `competitor`
- `field_name`
- `reason`
- `query_list`
- `priority`

Recommended UI:

- QA summary card
- Rework ticket / recollect instruction panel
- “Needs attention” section

## 4.6 `report`

Purpose:

- Human-readable report output

Fields:

- `markdown`
- `sources`

Recommended UI:

- Report preview/editor
- Source appendix list

Frontend note:

- `sources` is report-level source list
- For structured per-finding evidence display, use `artifacts.findings` and
  `artifacts.evidences`

## 4.7 `artifacts`

Purpose:

- Canonical structured-output section
- Use this to prove the system emits schema objects, not only markdown

Fields:

- `analysis_schema_plan`
- `evidences`
- `competitor_analyses`
- `profiles`
- `findings`
- `tickets`
- `report`

### `artifacts.analysis_schema_plan`

Use for:

- Schema tab
- Field planning view
- Field priority and query source display

### `artifacts.evidences`

Use for:

- Evidence table
- Source explorer
- Citation drill-down

Recommended fields to show if present:

- `source_url`
- `title`
- `snippet`
- `query`
- `claim_tags`
- `confidence`
- `credibility_score`
- `source_type`

### `artifacts.competitor_analyses`

Use for:

- Per-competitor field summary view
- Matrix or table grouped by product and field

### `artifacts.profiles`

Use for:

- Structured competitor profile rendering
- Feature tree
- Pricing model
- User feedback

This is one of the most important display sections for the judging criteria.

### `artifacts.findings`

Use for:

- Structured findings cards
- Evidence-backed conclusion view

Recommended UI:

- One finding card per item
- Show `statement`, `category`, `confidence`
- Render `evidence_refs` as evidence chips
- Resolve those refs against `artifacts.evidences`

### `artifacts.tickets`

Use for:

- Rework ticket display
- QA action history

### `artifacts.report`

Use for:

- Structured report object viewer
- Section-based report UI if desired

Frontend note:

- `report.markdown` is best for quick reading/editing
- `artifacts.report` is best for structured section rendering

## 4.8 `observability`

Purpose:

- Canonical observability container

Fields:

- `llm_calls`
- `stage_logs`
- `agent_traces`
- `events`
- `manual_interventions`
- `log_download_path`

### `observability.llm_calls`

Purpose:

- Global flat list of all LLM calls across the run

Use for:

- Global token statistics
- Cross-stage filtering
- Search across all prompts/calls

Recommended UI:

- Global trace table
- Token leaderboard

Frontend note:

- For stage/agent drill-down, prefer `observability.agent_traces`
- For global aggregations, prefer `observability.llm_calls`

### `observability.stage_logs`

Purpose:

- Per-stage raw observability bundle

Fields per stage:

- `io`
- `inputs`
- `outputs`
- `events`
- `handoffs`
- `llm_calls`

Recommended UI:

- Advanced debug drawer
- “Raw stage logs” tab

### `observability.agent_traces`

Purpose:

- Canonical per-agent execution-trace section

This is the preferred frontend source for showing:

- prompt
- input
- output
- internal stage steps
- token consumption
- multi-call agent behavior

Each item:

```json
{
  "stage": "plan",
  "agent_name": "Planner Agent",
  "status": "completed",
  "summary": {
    "llm_call_count": 4,
    "total_tokens": 4200,
    "prompt_tokens": 3000,
    "completion_tokens": 1200,
    "event_count": 6,
    "handoff_count": 1,
    "input_count": 1,
    "output_count": 1
  },
  "steps": [
    {
      "step_type": "input|event|llm_call|handoff|output",
      "display_name": "...",
      "created_at": "..."
    }
  ]
}
```

Recommended UI:

- One collapsible stage container per agent
- Summary header on top
- Internal execution timeline below

### How To Render Multiple LLM Calls For One Agent

One stage can contain multiple LLM calls. Do not flatten them into a single
cross-run trace list in the main workspace.

Use this rendering hierarchy:

1. Stage/agent card
2. Internal chronological step timeline
3. Expandable detail for one `llm_call`

Recommended stage header:

- stage name
- agent name
- status
- llm call count
- total tokens
- prompt tokens
- completion tokens

Recommended internal timeline order:

- `input`
- `event`
- `llm_call`
- `handoff`
- `output`

Recommended `llm_call` detail panel:

- `display_name`
- `trace_name`
- `status`
- `model`
- `system_prompt`
- `user_payload`
- `parsed_response`
- `raw_response`
- `latency_ms`
- `prompt_tokens`
- `completion_tokens`
- `total_tokens`
- `finish_reason`
- `error_reason`
- `error_message`
- `input_preview`
- `output_preview`

This is the best UI model for demos because it shows:

- the agent as the reasoning container
- the multiple LLM calls as internal steps
- exact prompts and outputs without overwhelming the default screen

### `observability.events`

Purpose:

- Full event history embedded in the snapshot

Use for:

- Event feed
- Runtime event pane

Frontend note:

- For live updates, prefer `/stream`
- For event history in a loaded workspace, this snapshot field is enough

### `observability.manual_interventions`

Purpose:

- Human intervention history

Fields:

- `node_name`
- `action`
- `before`
- `after`
- `reason`
- `actor`
- `created_at`

Recommended UI:

- Intervention timeline
- Audit panel
- Human-in-the-loop history

### `observability.log_download_path`

Purpose:

- Download/export entry point

Use for:

- Export button only

Do not:

- use this as the primary UI data source

## 5. Incremental Event APIs

## 5.1 `GET /runs/{run_id}/events?after_id=0&limit=200`

Purpose:

- Polling fallback
- Catch-up after SSE reconnect
- Replay/event history loading

Response:

```json
{
  "run_id": "run_xxx",
  "items": [
    {
      "event_id": 12,
      "stage": "collect",
      "event_type": "provider_event",
      "payload": {},
      "created_at": "..."
    }
  ],
  "next_after_id": 12,
  "has_more": false
}
```

Frontend usage:

- Persist `next_after_id`
- On reconnect, call with previous `after_id`
- Append `items` to live timeline

## 5.2 `GET /runs/{run_id}/stream`

Purpose:

- Live workspace update channel over SSE

SSE event types:

- `workspace`
- `run_event`
- `run_done`
- `heartbeat`
- `error`

### `workspace`

Payload:

```json
{
  "run_id": "run_xxx",
  "status": "running",
  "workspace": { "...full workspace payload..." }
}
```

Use for:

- replacing the in-memory workspace snapshot
- updating stage status, DAG, report, traces, schema flow, artifacts

### `run_event`

Payload:

- one item from the event stream

Use for:

- live event list
- lightweight status ticker

### `run_done`

Payload:

```json
{
  "run_id": "run_xxx",
  "status": "completed",
  "last_event_id": 123
}
```

Use for:

- final refresh trigger
- stopping live polling/stream indicators

### `heartbeat`

Use for:

- connection liveness only

### `error`

Use for:

- fallback to polling

Recommended frontend strategy:

1. load `workspace`
2. open SSE
3. update on `workspace`
4. append on `run_event`
5. final refresh on `run_done`
6. fallback to `GET /runs/{run_id}` + `GET /workspace` polling on stream failure

## 6. Replay and Drill-Down APIs

## 6.1 `GET /runs/{run_id}/replay`

Purpose:

- Replay page
- Debug-oriented historical view

Fields:

- `run_id`
- `status`
- `timeline`
- `handoffs`
- `llm_calls`

Recommended UI:

- Replay screen
- Judge-facing “how the run progressed” view

Frontend note:

- `workspace` is still better for the main UI
- `replay` is better for explicit replay/debug screens

## 6.2 `GET /runs/{run_id}/nodes/{node_name}`

Purpose:

- Focused single-stage drill-down

Response contains:

- `run_id`
- `node_name`
- `io`
- `handoffs`
- `llm_calls`

Recommended UI:

- “Open stage detail” drawer
- Dedicated node debug modal

Best use:

- when a user clicks one stage in the DAG and wants raw details

## 6.3 `GET /runs/{run_id}/logs/export`

Purpose:

- Download/export full logs

Use for:

- export button
- debug attachment

Do not:

- make this the main screen payload

## 7. Operations APIs

## 7.1 `POST /runs/{run_id}/ops/resume`

Purpose:

- Resume from latest checkpoint

Frontend usage:

- recovery action
- retry flow from paused or interrupted state

## 7.2 `POST /runs/{run_id}/ops/intervene`

Purpose:

- Human-in-the-loop patch/intervention

Body example:

```json
{
  "node_name": "plan",
  "action": "edit_schema",
  "actor": "judge",
  "reason": "manual approve",
  "patch": {
    "analysis_schema_plan": []
  }
}
```

Frontend usage:

- intervention modal
- human approval/edit action
- advanced demo controls

Recommended UI fields:

- actor
- reason
- action
- patch preview

## 8. Preview and Readiness APIs

## 8.1 `POST /collector/preview`

Purpose:

- Pre-run display before full execution
- Show inferred industry, planned competitors, schema plan preview

Recommended UI:

- launch preview
- “what the planner inferred” panel

Useful fields:

- `prompt`
- `inferred_industry`
- `planned_competitors`
- `analysis_schema_plan`
- `execution_timeline`
- `preview`
- `planner_meta`

Frontend note:

- This is not the main run workspace
- Use this before starting a run, or as a lightweight preview step

## 8.2 `GET /collector/providers/health`

Purpose:

- Display provider readiness/system status

Recommended UI:

- settings page
- provider health section
- pre-demo readiness checklist

## 8.3 `GET /collector/llm/health`

Purpose:

- Show whether LLM configuration is reachable/healthy

Recommended UI:

- system status page
- environment readiness check

## 9. Schema and Runtime Interfaces

## 9.1 `GET /schema/registry`

Purpose:

- Display active schema registry / industry schema info

Recommended UI:

- schema registry page
- schema comparison/configuration drawer

## 9.2 `GET /schema/runtime-config`

Purpose:

- Show masked runtime configuration

Recommended UI:

- admin/system settings page
- “runtime ready?” banner

Useful fields include:

- model selection
- base URL
- masked API key info
- readiness booleans

## 10. UI Mapping Summary

Use this table when deciding which backend payload to bind to which component.

### Run launcher

- `POST /collector/preview`
- `POST /runs`

### Recent runs sidebar

- `GET /runs`

### Workspace header

- `workspace.summary`
- `workspace.run`
- `workspace.request`

### Main DAG board

- `workspace.workflow.dag`
- `workspace.workflow.agent_stages`
- `workspace.workflow.timeline`

### Stage subflow panel

- `workspace.workflow.agent_workflows`

### Schema flow / handoff panel

- `workspace.workflow.agent_handoffs`
- fallback/debug: `workspace.workflow.handoffs`

### QA panel

- `workspace.qa`
- `workspace.artifacts.tickets`

### Report panel

- `workspace.report`
- optional structured view: `workspace.artifacts.report`

### Structured schema output tabs

- `workspace.artifacts.analysis_schema_plan`
- `workspace.artifacts.evidences`
- `workspace.artifacts.competitor_analyses`
- `workspace.artifacts.profiles`
- `workspace.artifacts.findings`

### Agent observability detail

- `workspace.observability.agent_traces`

### Global trace table

- `workspace.observability.llm_calls`

### Raw stage debug panel

- `workspace.observability.stage_logs`
- or `GET /runs/{run_id}/nodes/{node_name}`

### Live event feed

- `/runs/{run_id}/stream`
- `/runs/{run_id}/events`
- snapshot fallback: `workspace.observability.events`

### Human intervention history

- `workspace.observability.manual_interventions`

### Export logs button

- `workspace.observability.log_download_path`
- `GET /runs/{run_id}/logs/export`

### Readiness/system status page

- `GET /collector/providers/health`
- `GET /collector/llm/health`
- `GET /schema/runtime-config`

## 11. Canonical vs Secondary Sources

Use these preferred data sources unless you have a specific reason not to.

### Canonical

- Main workspace: `GET /runs/{run_id}/workspace`
- Structured outputs: `workspace.artifacts`
- Schema flow: `workspace.workflow.agent_handoffs`
- Per-agent execution trace: `workspace.observability.agent_traces`
- Live updates: `GET /runs/{run_id}/stream`

### Secondary / debug

- Flat llm list: `workspace.observability.llm_calls`
- Full replay: `GET /runs/{run_id}/replay`
- Node-specific debug: `GET /runs/{run_id}/nodes/{node_name}`
- Export logs: `GET /runs/{run_id}/logs/export`

## 12. Integration Notes For Coding Agents

- The backend already aggregates a frontend-friendly workspace snapshot. Prefer
  using that snapshot instead of reconstructing state from raw endpoints.
- Keep large payloads collapsed by default. Raw JSON is available, but the main
  UX should prioritize summaries, highlights, and previews.
- For stage detail, do not flatten all prompts across the run. Group by
  `observability.agent_traces[].stage`.
- For schema correctness demos, use `workspace.artifacts` plus
  `workflow.agent_handoffs`.
- For observability demos, use `observability.agent_traces` first and
  `observability.llm_calls` second.
