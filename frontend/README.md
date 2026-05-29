# Frontend Demo

这个前端是给 `Competitor_Agent` 的后端演示台，目标是：

- 默认使用稳定的 `mock_data` 做演示
- 可切换到真实后端 API 读取运行态
- 一屏覆盖评分细则里的核心给分点

## 技术栈

- React 18
- TypeScript
- Vite

## 启动

```bash
cd frontend
npm install
npm run dev -- --host 127.0.0.1
```

默认访问：

- `http://127.0.0.1:4173`

## 数据模式

### Mock Mode

默认读取这些静态数据：

- `mock_data/complete_flow_result/complete_flow_result.json`
- `mock_data/complete_flow_result/final_report_20260528_210348.md`
- `mock_data/complete_flow_result/qa_rework_result_20260528_210348.json`
- `mock_data/complete_flow_result/analyst_output/*.json`

适合现场演示，稳定、不依赖后端状态。

### API Mode

通过 Vite proxy 访问：

- `/runs`
- `/runs/{run_id}`
- `/runs/{run_id}/replay`
- `/runs/{run_id}/ops/intervene`
- `/collector/*`
- `/schema/*`

默认代理到：

- `http://127.0.0.1:8000`

## 页面覆盖点

- Run Overview：效率、覆盖度、QA 重做指标
- Agent Roles：多 Agent 分工与协议
- Workflow Replay：DAG、handoff、决策回放
- Competitor Profile / Field Analysis：Schema-first 输出
- Findings & Traceability：结论与来源入口
- QA Loop：真实打回与补采项
- Intervention：人工修正入口
- Observability：LLM trace / token / 决策过程
- Report Viewer：最终报告展示
