# Competitor Analysis

<p align="center">
  <img src="https://img.shields.io/badge/Multi--Agent-Workflow-2563eb?style=for-the-badge&logo=openai&logoColor=white" alt="Multi-Agent Workflow" />
  <img src="https://img.shields.io/badge/FastAPI-Backend-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI Backend" />
  <img src="https://img.shields.io/badge/Next.js-Frontend-111111?style=for-the-badge&logo=nextdotjs&logoColor=white" alt="Next.js Frontend" />
  <img src="https://img.shields.io/badge/PostgreSQL-Storage-336791?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL Storage" />
  <img src="https://img.shields.io/badge/Redis-Streaming%20%26%20Cache-dc2626?style=for-the-badge&logo=redis&logoColor=white" alt="Redis Streaming and Cache" />
</p>

<p align="center">
  <b>一个面向竞品调研场景的多智能体分析系统</b><br/>
  从采集、规划、分析、质检到报告输出，完成竞品分析全链路自动化。
</p>

## 项目简介

`Competitor_Analysis` 是一个 AI 驱动的竞品分析协作系统，目标是把传统人工竞品分析中的重复性工作自动化。系统围绕“规划分析范围 -> 采集公开信息 -> 结构化分析 -> QA 回流 -> 生成报告 -> 问卷导出”这一真实业务链路构建，既能产出结果，也能保留中间过程、引用来源和回放能力，是一个包含后端工作流、前端工作台、事件流、缓存、持久化和人工介入能力的完整工程项目。

## 项目实现了什么

| 能力 | 说明 |
| --- | --- |
| `🧠` 多智能体协作 | 将 Planner、Collector、Analyst、Writer、QA 等角色拆分为独立职责，按阶段推进任务。 |
| `🗂️` 结构化竞品分析 | 围绕 schema 规划分析字段，而不是只输出自由文本，便于复用、比较与追溯。 |
| `🌐` 采集预览与深度调研 | 支持 prompt 驱动的候选竞品识别、字段规划、handoff 目标生成，以及 deep-dive 采集。 |
| `🔍` 证据溯源 | 分析结论与采集证据关联，强调报告可追溯，不是黑盒生成。 |
| `🧪` QA 回流机制 | 质检阶段可以识别缺口并回推采集或分析阶段，形成闭环而不是一次性输出。 |
| `📄` 报告编辑与导出 | 支持 Markdown 报告生成、在线修订、问卷设计与问卷星导出。 |
| `📡` SSE 实时工作流 | 前端通过流式事件订阅后端运行状态，动态展示 run、workspace 和阶段变化。 |
| `🧰` 人工介入与回放 | 支持 checkpoint 恢复、手工干预、事件回放、日志导出，便于调试和演示。 |

## 亮点

### `✨` 亮点 1：不是单 Agent，而是完整多阶段工作流

后端工作流围绕以下阶段组织：

`plan -> confirm_plan -> collect -> normalize -> analyze -> draft -> qa -> finalize`

这意味着系统并不是直接把 prompt 丢给模型生成报告，而是先规划分析对象和字段，再采集证据、结构化分析、做 QA 检查，最后输出报告。

### `✨` 亮点 2：过程可见，而不是只看结果

项目提供前端工作台和 SSE 实时流，用于展示：

- run 当前状态
- agent 阶段变化
- event 流
- workspace 数据
- report / questionnaire 更新
- 回放与日志导出

这让系统更像一个可操作、可审计的产品，而不是纯脚本。

### `✨` 亮点 3：支持结构化输出和后续复用

系统强调 schema-first 设计。它不仅生成最终报告，还会沉淀：

- 分析字段规划
- 证据集合
- findings
- competitor profiles
- 报告 markdown
- 问卷设计结果

因此既适合一次性分析，也适合后续扩展成知识资产。

### `✨` 亮点 4：工程完整度高

项目当前已经具备：

- FastAPI 后端 API
- Next.js 前端工作台
- PostgreSQL 持久化
- Redis 缓存与流式消息支持
- Docker Compose 一键拉起后端运行栈
- 本地开发模式与生产部署基础

## 核心功能模块

### `🖥️` 前端工作台

前端位于 `frontend/`，基于 Next.js + React，主要用于：

- 创建分析任务
- 展示 agent 流程与运行工作区
- 订阅 `GET /runs/{run_id}/stream` 的 SSE 实时流
- 查看 workspace、事件、报告与问卷结果

### `⚙️` 后端工作流

后端位于 `backend/`，基于 FastAPI、多智能体工作流、PostgreSQL、Redis，主要提供：

- `POST /collector/preview`：采集预览
- `POST /runs`：异步启动分析任务
- `GET /runs/{run_id}/stream`：SSE 流式事件
- `GET /runs/{run_id}/workspace`：前端工作台数据
- `PATCH /runs/{run_id}/report`：报告修订
- `POST /runs/{run_id}/questionnaire/export/wenjuan`：问卷导出

### `🧭` 工作流阶段

| 阶段 | 说明 |
| --- | --- |
| `plan` | 识别分析对象、规划分析字段和任务边界 |
| `confirm_plan` | 等待用户确认或补充需求 |
| `collect` | 采集证据并按字段组织 |
| `normalize` | 对采集结果做规范化处理 |
| `analyze` | 生成结构化结论、竞品画像和比较结果 |
| `draft` | 产出可编辑 Markdown 报告 |
| `qa` | 检查完整性、引用与缺口，必要时打回重做 |
| `finalize` | 结束任务并输出最终状态 |

## 技术栈

### `🔧` 后端

- `FastAPI`
- `Pydantic`
- `PostgreSQL`
- `Redis`
- `LangGraph` 风格工作流运行时
- OpenAI-compatible LLM runtime

### `🎨` 前端

- `Next.js`
- `React`
- SSE 实时订阅

### `🐳` 部署

- `Docker`
- `Docker Compose`
- `uv`
- `pnpm / npm`

## 仓库结构

```text
Competitor_Analysis/
├─ backend/      # FastAPI 后端、多智能体工作流、Docker 部署配置
├─ frontend/     # Next.js 前端工作台
├─ documents/    # 项目文档与资料
├─ mock_data/    # 本地示例/调试数据
├─ README.md     # 项目主页说明
└─ 竞品分析.md    # 项目背景与方案说明
```

## 快速预览

### `📌` 适合展示的能力

- 自动识别候选竞品和分析字段
- 对工作流执行过程做实时展示
- 生成结构化 Markdown 报告
- 基于报告继续生成问卷
- 保留日志、事件、trace、回放与人工介入能力

### `📌` 适合落地的场景

- 产品团队竞品调研
- 行业研究与市场分析
- 功能对比报告生成
- 调研结果复盘与共享
- AI Agent 工作流演示项目

## 本地开发启动

### `1.` 启动前准备

- 安装后端依赖目录：`backend`
- 安装前端依赖目录：`frontend`
- 确保已配置环境变量文件：
  - `Competitor_Analysis/.env`
  - `Competitor_Analysis/backend/.env`

### `2.` 启动后端

在 `backend` 目录执行：

```bash
uv run uvicorn app.main:app --reload --port 8010
```

后端默认地址：

```text
http://127.0.0.1:8010
```

### `3.` 启动前端

在 `frontend` 目录执行：

```bash
npm run dev
```

前端默认地址：

```text
http://127.0.0.1:3000
```

## 部署说明

### `🐳` Docker 一键启动后端

后端已经提供完整的 Docker 部署配置，进入 `backend/` 目录后可直接启动：

```bash
cd backend
cp ".env example" .env
docker compose up -d --build
```

默认会启动以下服务：

- `backend`：FastAPI 服务，端口 `8010`
- `postgres`：PostgreSQL，宿主机端口 `5433`
- `redis`：Redis，宿主机端口 `6379`

启动完成后可通过以下命令检查状态：

```bash
docker compose ps
curl http://127.0.0.1:8010/healthz
docker compose logs -f backend
```

停止服务：

```bash
docker compose down
```


### `🚀` 前端部署

前端位于 `frontend/`，可本地开发运行，也可以部署到 Vercel 或其他支持 Next.js 的平台。生产环境建议通过服务端代理把 `/runs`、`/collector`、`/schema` 转发到后端，减少跨域和 mixed content 问题。

### `📚` 详细文档

- 后端说明：[`backend/README.md`](./backend/README.md)
- 前端说明：[`frontend/README.md`](./frontend/README.md)
- 项目背景：[`竞品分析.md`](./竞品分析.md)
