# API 接口文档

## 1. 文档概述

本项目围绕“竞品分析任务”提供了一组完整的后端接口，支持从任务创建、运行过程查看，到报告修订、问卷生成与问卷导出的整条使用链路。  
这些接口对应的不是零散功能点，而是一套完整的竞品分析工作流：用户提交研究任务后，系统会自动完成规划、采集、分析、质检和报告生成，随后还支持继续追问和问卷验证。

整体接口设计强调三点：

- 入口统一，便于前端工作台和后续系统集成调用；
- 返回结构清晰，便于展示运行状态、分析结果和证据来源；
- 支持流式交互，便于实时展示多 Agent 协作过程。

## 2. 接口体系说明

从使用方式看，系统接口主要分为五组：

1. 运行管理接口  
用于创建竞品分析任务、查询运行状态、获取事件流和工作区快照。

2. 报告交互接口  
用于获取、下载、修订报告，并围绕既有报告继续发起追问。

3. 问卷接口  
用于根据竞品分析报告生成问卷，并导出到问卷星。

4. 采集预览接口  
用于在正式运行前预览行业识别、竞品发现和分析字段规划结果。

5. Schema 与配置接口  
用于查看当前可用的分析 Schema 注册表和运行时配置。

## 3. 接口总览

### 3.1 运行管理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/runs` | 创建竞品分析任务 |
| POST | `/runs/summary` | 对任务描述进行摘要整理 |
| GET | `/runs` | 获取任务运行列表 |
| GET | `/runs/{run_id}` | 获取单个运行状态 |
| DELETE | `/runs/{run_id}` | 删除运行记录 |
| GET | `/runs/{run_id}/events` | 获取运行事件列表 |
| GET | `/runs/{run_id}/replay` | 获取完整运行回放数据 |
| GET | `/runs/{run_id}/workspace` | 获取工作区快照 |
| GET | `/runs/{run_id}/stream` | 订阅运行期 SSE 事件流 |

### 3.2 报告与追问

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/runs/{run_id}/report.md` | 下载 Markdown 报告 |
| PATCH | `/runs/{run_id}/report` | 更新报告内容 |
| POST | `/runs/{run_id}/chat` | 发起报告追问 |
| GET | `/runs/{run_id}/chat` | 获取报告对话记录 |
| GET | `/runs/{run_id}/chat/{turn_id}` | 获取单轮追问结果 |
| GET | `/runs/{run_id}/chat/{turn_id}/stream` | 订阅单轮追问的流式输出 |

### 3.3 问卷生成与导出

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/runs/{run_id}/questionnaire` | 基于报告生成问卷 |
| PATCH | `/runs/{run_id}/questionnaire` | 更新问卷内容 |
| POST | `/runs/{run_id}/questionnaire/export/wenjuan` | 导出问卷到问卷星 |

### 3.4 采集预览与健康检查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/collector/preview` | 预览竞品发现与分析字段规划 |
| GET | `/collector/providers/health` | 检查采集 Provider 状态 |
| GET | `/collector/llm/health` | 检查采集链路中的模型状态 |

### 3.5 Schema 与运行配置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/schema/registry` | 获取分析 Schema 注册表 |
| GET | `/schema/runtime-config` | 获取运行时配置的脱敏视图 |

## 4. 关键接口详细说明

### 4.1 创建竞品分析任务

**接口**

`POST /runs`

**作用**

创建一条新的竞品分析任务。任务创建成功后，系统会异步进入多阶段工作流，依次完成任务规划、证据采集、结构化分析、质量审查和报告生成。

**典型输入**

```json
{
  "prompt": "分析通用 AI 智能体平台的主要竞品，重点关注产品能力、定价和用户反馈",
  "industry_hint": "AI Agent",
  "competitor_hints": ["OpenAI", "Anthropic"],
  "focus_points": ["产品能力", "价格策略", "用户评价"]
}
```

**典型输出**

```json
{
  "run_id": "run_xxx",
  "status": "queued"
}
```

**说明**

- `prompt` 为必填项，用于描述分析任务。
- 其余字段用于补充行业、竞品线索和关注维度，帮助系统更快形成合理的分析规划。

### 4.2 获取工作区快照

**接口**

`GET /runs/{run_id}/workspace`

**作用**

返回竞品分析工作台展示所需的聚合数据，是最核心的读取接口之一。  
它不仅返回任务状态，还会同时返回报告内容、阶段进度、事件摘要、分析字段、QA 结果和问卷信息。

**主要信息类型**

- 任务摘要与当前状态
- 阶段状态与阶段说明
- 动态 Schema 规划结果
- 工作区事件与可观测信息
- 报告内容与报告修订状态
- QA 检查结果与返工信息
- 问卷内容与导出结果

**适用场景**

- 打开历史任务
- 页面刷新后恢复完整状态
- 流式连接中断后的状态补全

### 4.3 订阅运行期事件流

**接口**

`GET /runs/{run_id}/stream`

**作用**

通过 `SSE` 持续返回运行过程中的阶段更新、工作区更新和结束状态，使使用者能够实时看到系统如何从输入任务逐步推进到输出报告。

**主要事件类型**

- `workspace`：工作区全量更新
- `run_event`：运行事件增量更新
- `task_summary`：任务摘要更新
- `run_done`：任务结束
- `error`：异常状态

**使用价值**

- 展示多 Agent 协作过程
- 展示 QA 打回与返工过程
- 支撑报告生成过程中的实时可视化体验

### 4.4 报告下载与修订

**接口**

- `GET /runs/{run_id}/report.md`
- `PATCH /runs/{run_id}/report`

**作用**

支持对已生成的报告进行下载、保存和继续修订。  
这使系统输出不再是一次性结果，而是可持续完善的研究产物。

### 4.5 报告追问与流式回答

**接口**

- `POST /runs/{run_id}/chat`
- `GET /runs/{run_id}/chat`
- `GET /runs/{run_id}/chat/{turn_id}`
- `GET /runs/{run_id}/chat/{turn_id}/stream`

**作用**

支持围绕已有报告继续提问，例如追问某一结论的依据、要求改写某一章节，或补充某个竞品信息。  
系统会基于已有报告、工作记忆和必要的补充检索继续回答，并支持流式输出。

**主要事件类型**

- `chat_bootstrap`
- `chat_snapshot`
- `chat_progress`
- `chat_done`
- `chat_error`
- `heartbeat`

### 4.6 采集预览

**接口**

`POST /collector/preview`

**作用**

在正式执行完整分析链路之前，先根据任务描述返回行业识别结果、候选竞品和分析字段规划，帮助使用者提前判断系统将如何理解并执行该任务。

**典型价值**

- 提前验证竞品范围是否合理
- 提前查看分析维度是否覆盖目标问题
- 演示系统的自动行业识别和动态 Schema 规划能力

### 4.7 问卷生成与导出

**接口**

- `POST /runs/{run_id}/questionnaire`
- `PATCH /runs/{run_id}/questionnaire`
- `POST /runs/{run_id}/questionnaire/export/wenjuan`

**作用**

系统能够将竞品分析报告进一步转换为用户调研问卷，并在确认后导出为问卷星链接，形成“分析结论 -> 用户验证”的延伸链路。

**导出结果通常包含**

- `provider`
- `status`
- `title`
- `url`
- `vid`
- `exported_at`

## 5. 推荐使用路径

若从完整业务链路理解本系统接口能力，建议按以下顺序使用：

1. 调用 `POST /runs` 创建分析任务
2. 通过 `GET /runs/{run_id}/workspace` 查看工作区快照
3. 通过 `GET /runs/{run_id}/stream` 观察实时阶段推进
4. 在报告生成后，使用 `POST /runs/{run_id}/chat` 继续追问
5. 在报告确认后，使用 `POST /runs/{run_id}/questionnaire` 生成问卷
6. 最后使用 `POST /runs/{run_id}/questionnaire/export/wenjuan` 导出问卷链接

## 6. 设计特点总结

本项目接口设计具有以下特点：

- 围绕竞品分析任务完整链路设计，而不是零散工具接口集合
- 支持结构化工作区快照，便于前端统一展示
- 支持 `SSE` 流式事件，能够真实反映系统运行过程
- 支持报告后的持续修订和问卷扩展，增强结果复用价值
- 支持采集预览、Schema 查看和质量闭环，体现系统工程化能力
