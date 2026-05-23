# 竞品分析后端

这是一个受 Deer-Flow 启发的多智能体竞品分析工作流后端 v1。

## 运行

```bash
cd backend
uv run uvicorn app.main:app --reload --port 8010
```

## LLM 配置（仅 OPENAI 兼容）

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_MODEL`

`backend` 不再读取 `ARK_*` 环境变量。

## 核心能力

- 编排阶段：Plan -> Collect -> Normalize -> Analyze -> Draft -> QA -> Finalize
- 以 Schema 为先的契约：Evidence、Finding、Report、ReworkTicket
- 可按行业扩展的 schema 注册表（核心字段 + 行业扩展字段）
- 基于 QA 的返工闭环（结构化工单）
- 基于 SQLite 的 run/event/ticket 持久化与回放 API
- 可选的 schema 演进提案生成

## Collector 环境变量控制项

- `COLLECTOR_MAX_URLS`：预览执行 URL 上限，由服务端 `.env` 控制。
- `COLLECTOR_PER_FIELD_LIMIT`：每个 schema 字段的证据上限（实验限流可设为 `1`）。
- `/collector/preview` 执行时会使用服务端 `COLLECTOR_MAX_URLS` 与 `COLLECTOR_PER_FIELD_LIMIT` 做限流。
- `COLLECTOR_PREVIEW_AUTO_SAVE_ENABLED`：是否将每次 `/collector/preview` 响应自动保存为本地 JSON（默认 `true`）。
- `COLLECTOR_PREVIEW_SAVE_DIR`：自动保存目录（默认是 backend 工作目录下的 `collector_exports`）。
- 自动保存文件名格式：`collector_preview_result_YYYYMMDD_HHMMSS_<6hex>.json`。

## 仅 Prompt 预览

`POST /collector/preview` 现在支持仅传 prompt 的请求，并返回可交接的 JSON。

```bash
curl -X POST "http://127.0.0.1:8010/collector/preview" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "通用AI智能体竞品分析",
    "industry_hint": "",
    "competitor_hints": []
  }'
```

响应包含：
- 仅 `candidates.direct` 和 `candidates.substitute`（不包含 `irrelevant`）
- `analysis_schema_plan`（动态 schema，强制包含核心字段）
- 面向下游深度分析智能体的 `handoff_targets`

## PowerShell 命令（不使用 curl）

如果你使用的是 Windows PowerShell，建议优先使用 `Invoke-RestMethod`（或 `Invoke-WebRequest`），不要用 `curl`。

### 1）健康检查

```powershell
$base = "http://127.0.0.1:8010"
Invoke-RestMethod -Method Get -Uri "$base/health"
```

### 2）仅 Prompt 预览

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
