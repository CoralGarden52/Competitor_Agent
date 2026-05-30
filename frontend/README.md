# Competitor Analysis Frontend

基于 Next.js + React 的竞品分析工作台页面，用来连接后端多智能体流程并展示运行态、事件流和最终报告。

## 功能

- 左侧导航（新建任务 / Agent 流程 / 历史运行）
- 中央工作台展示 DAG、handoff、事件流、QA 回路和报告
- 通过后端 API 启动异步 run，并优先使用 SSE 实时刷新工作区

## 启动方式

```bash
cd frontend
pnpm install
pnpm dev
```

默认访问：

- `http://localhost:3000`

如果后端不是默认的 `http://127.0.0.1:8010`，先配置：

```bash
export NEXT_PUBLIC_BACKEND_URL=http://127.0.0.1:8010
```

## 联调说明

- 前端调用 `POST /runs/async` 启动异步任务
- 前端优先订阅 `GET /runs/{run_id}/stream` 的 SSE 实时流来动态展示工作区
- `GET /runs/{run_id}/workspace` 用于首屏加载和手动刷新兜底
- `GET /runs/{run_id}/logs/export` 可导出运行日志

## 生产构建

```bash
pnpm build
pnpm start
```
