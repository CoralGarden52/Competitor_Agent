# 竞品分析后端

该服务为 `Competitor_Analysis` 项目提供后端工作流能力，包含采集预览、分析任务启动、进度流式推送、报告编辑以及问卷导出等 FastAPI 接口。当前运行时基于 PostgreSQL、Redis、FastAPI，以及在 `app/core/workflow.py` 中编排的多智能体工作流。

## 技术栈

- FastAPI 应用入口：`app/main.py`
- 运行时依赖装配：`app/core/deps.py`
- 工作流编排核心：`app/core/workflow.py`
- 持久化存储：`app/core/storage.py`
- LLM / 采集 / 智能体模块：`app/agents`、`app/core/collector`、`harness/tools`
- 容器启动配置：`Dockerfile` 和 `docker-compose.yml`

## 目录概览

- `app/main.py`：创建 FastAPI 应用、挂载路由、启用 CORS，并暴露 `GET /healthz`
- `app/api/runs.py`：任务创建、任务流、工作台、聊天、报告、问卷和运维接口
- `app/api/collector.py`：采集预览与 Provider 健康检查接口
- `app/api/schema.py`：Schema 注册表和脱敏运行时配置接口
- `app/core/workflow.py`：组装 planner、collector、analyst、writer、QA、缓存和子代理运行时
- `app/core/storage.py`：PostgreSQL 初始化，以及 run / event / checkpoint / trace 持久化
- `app/core/config.py`：从项目根目录与 `backend/.env` 读取 `.env`

## Docker 部署

### 1. 准备环境变量

在 `backend` 目录下执行：

```bash
cd Competitor_Analysis/backend
cp ".env example" .env
```

### 2. 启动完整后端栈

```bash
cd Competitor_Analysis/backend
docker compose up -d --build
```

### 3. 验证部署结果

```bash
docker compose ps
curl http://127.0.0.1:8010/healthz
docker compose logs -f backend
```

期望的健康检查响应：

```json
{"status":"ok"}
```

### 4. 停止或重建

```bash
docker compose down
docker compose up -d --build
```

### 5. 持久化数据

当前 Docker 卷包括：

- `backend_data`：后端运行产物，例如采集预览导出和问卷导出文件
- `backend_logs`：后端日志文件
- `postgres_data`：PostgreSQL 数据文件
- `redis_data`：Redis AOF 持久化数据

### 6. 服务器部署说明

在 Linux 服务器上：

```bash
git clone <your-repo-url>
cd Competitor_Analysis/backend
cp ".env example" .env
docker compose up -d --build
```

常用运维命令：

```bash
docker compose ps
docker compose logs -f backend
docker compose pull
docker compose up -d --build
```

## 本地开发启动

如果不使用 Docker，请自行准备 PostgreSQL 和 Redis，然后执行：

```bash
cd Competitor_Analysis/backend
uv sync
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8010
```

`app/core/config.py` 中的重要本地默认值：

- PostgreSQL 默认主机为 `localhost`
- PostgreSQL 默认端口为 `5433`
- Redis 在代码中默认关闭，但在 Docker Compose 中通过环境变量启用

## API 概览

### 健康检查与运行时信息

- `GET /healthz`：服务健康检查
- `GET /schema/runtime-config`：脱敏后的运行时配置
- `GET /schema/registry`：当前激活的 Schema 注册表快照

### 采集预览

- `POST /collector/preview`：根据输入 prompt 生成采集候选、schema 规划和下游 handoff 目标
- `GET /collector/providers/health`：Provider 健康状态汇总
- `GET /collector/llm/health`：collector 使用的 LLM 健康状态

示例：

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

### 工作流任务

- `POST /runs`：异步创建分析任务
- `GET /runs`：查询任务列表
- `GET /runs/{run_id}`：获取任务状态
- `GET /runs/{run_id}/stream`：通过 SSE 推送任务事件
- `GET /runs/{run_id}/workspace`：获取前端工作台数据
- `GET /runs/{run_id}/events`：查询持久化事件
- `GET /runs/{run_id}/replay`：回放任务执行时间线
- `POST /runs/{run_id}/ops/resume`：从最近 checkpoint 恢复
- `POST /runs/{run_id}/ops/intervene`：人工干预任务

### 计划确认与报告编辑

- `GET /runs/{run_id}/plan-confirmation`
- `POST /runs/{run_id}/plan-confirmation/confirm`
- `POST /runs/{run_id}/plan-confirmation/supplement`
- `PATCH /runs/{run_id}/report`
- `GET /runs/{run_id}/report.md`

### 聊天与问卷

- `POST /runs/{run_id}/chat`
- `GET /runs/{run_id}/chat`
- `GET /runs/{run_id}/chat/{turn_id}`
- `GET /runs/{run_id}/chat/{turn_id}/stream`
- `POST /runs/{run_id}/questionnaire`
- `PATCH /runs/{run_id}/questionnaire`
- `POST /runs/{run_id}/questionnaire/export/wenjuan`

## 后端逻辑实现

### 1. 应用启动

`app/main.py` 创建 FastAPI 应用，并注册：

- `runs_router`
- `schema_router`
- `collector_router`

同时启用宽松 CORS，并暴露 `GET /healthz`。

### 2. 依赖装配

`app/core/deps.py` 通过 `get_service()` 构建单例 `CompetitorWorkflowService`：

- 读取环境变量配置
- 初始化 Redis 运行时和 `WorkflowCache`
- 创建 `PostgresStore`
- 根据配置选择 `RedisChatStreamBroker` 或 `InMemoryChatStreamBroker`
- 返回 `CompetitorWorkflowService`

### 3. 存储初始化

`app/core/storage.py` 不只是简单 CRUD，它还负责：

- 自动创建目标 PostgreSQL 数据库（如果尚不存在）
- 启动时初始化所有所需表
- 持久化 run、event、stage handoff、checkpoint、LLM trace、聊天记录、报告修订和子代理运行记录
- 为前端工作台接口提供回放与审计数据

这意味着当前后端启动流程不需要额外的 migration 容器。

### 4. 工作流编排

核心运行时位于 `app/core/workflow.py`。`CompetitorWorkflowService` 会组装：

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

主要任务生命周期如下：

1. 接收 `RunRequest`
2. 初始化 `RunState`
3. 保存任务并写入初始事件
4. 异步执行工作流
5. 持久化阶段输入、输出、handoff、事件和 checkpoint
6. 通过 SSE 和 workspace API 暴露进度
7. 支持报告编辑、QA 回流、问卷生成与导出

### 5. 阶段模型

后端围绕以下阶段组织：

- `plan`
- `confirm_plan`
- `collect`
- `normalize`
- `analyze`
- `draft`
- `qa`
- `finalize`

`workflow.py` 中描述的阶段职责包括：

- `plan`：识别分析对象与 schema 字段
- `confirm_plan`：等待用户确认或补充
- `collect`：采集证据并归入字段
- `analyze`：将证据转换为结论和竞品画像
- `draft`：生成可编辑 Markdown 报告
- `qa`：检查完整性、引用和 unknown 缺口
- `finalize`：将任务标记为完成或失败

### 6. 采集链路

`POST /collector/preview` 会调用 `service.collector_preview(...)`，入参包括：

- `prompt`
- `industry_hint`
- `competitor_hints`
- `deep_dive`

该预览链路用于：

- 推断候选竞品
- 构建 `analysis_schema_plan`
- 返回 `handoff_targets`
- 按需触发 deep-dive 采集
- 按配置自动保存预览 JSON

### 7. 流式推送与可观测性

后端通过以下 SSE 接口支持前端实时更新：

- `GET /runs/{run_id}/stream`
- `GET /runs/{run_id}/chat/{turn_id}/stream`

进度数据来自多种来源：

- PostgreSQL 中持久化的事件
- Redis 启用时的 run stream 队列
- 基于 run state、trace、handoff 和 QA 元数据生成的 workspace 数据

### 8. 报告与问卷流程

分析完成后：

- writer agent 生成 Markdown 报告内容
- 报告可通过 `PATCH /runs/{run_id}/report` 修改
- questionnaire agent 可基于报告生成问卷
- 启用后可导出到问卷星

## 常用命令

### PowerShell 健康检查

```powershell
$base = "http://127.0.0.1:8010"
Invoke-RestMethod -Method Get -Uri "$base/healthz"
```

### PowerShell 采集预览

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

## 说明

- 当前后端以 PostgreSQL 作为主要运行时存储。
- Redis 在代码层面是可选的，但在 Docker Compose 中默认启用，用于缓存和流式支持。
- 健康检查端点是 `/healthz`，不是 `/health`。
- 本说明基于当前 `Competitor_Analysis/backend` 目录下代码状态整理。
