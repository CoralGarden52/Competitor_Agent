长上下文分片通常是：

1. 把大输入拆成多个 chunk，分别请求（每次更短）。 
2. 每次请求只带“局部上下文 + 必要全局约束”（不是共享同一个完整上下文窗口）。 
3. 再做聚合/归并（map-reduce、refine、投票等）得到最终结果。

 

所以它的核心是“**分而治之 + 汇总**”，不是多个请求共享同一个上下文空间。 

如果需要“看起来连续”，要靠额外传递摘要、状态或中间结构化结果。

 

**按 s01-s19** **的“****面面俱到”****改造清单（逐章落地）**

1. s01 Agent Loop：把“阶段函数调用”抽象成统一循环状态机（turn、transition_reason、recovery_state），所有节点都走同一续行语义。
2. s02 Tool Use：建立 ToolSpec(给模型) 与 Handler(执行器) 分离；统一工具路由，不让 agent 直接散调函数。
3. s03 TodoWrite：新增会话级计划状态（任务列表、in_progress、completed），前端可见并可回放。
4. s04 Subagent：把“采集深挖/证据交叉验证”改成隔离上下文子代理执行器（独立 messages、独立预算）。
5. s05 Skills：做技能注册与按需加载（先轻发现，再深注入），避免把全部领域知识硬塞 system prompt。
6. s06 Context Compact：三层压缩（短窗裁剪/中窗摘要/长窗归档），并显式保留“下一步工作记忆”。
7. s07 Permission：实现 deny -> mode -> allow -> ask 权限管道，特别是 bash/HTTP/文件写入 高风险动作。
8. s08 Hook：加生命周期 Hook 点（before_llm、before_tool、after_tool、after_stage、on_error），扩展逻辑不改主循环。
9. s09 Memory：加跨会话 memory（只存“不能从当前任务重建”的信息），并加写入策略与清理策略。
10. s10 Prompt Pipeline：把提示词改为流水线组装（系统策略、权限模式、memory 摘要、阶段目标、动态约束）。
11. s11 Recovery：错误恢复状态机化（continue/compact/backoff/fail）+ 每类重试预算，避免无限重试或直接崩。
12. s12 Task System：把 run 内 todo 升级为持久任务图（依赖、阻塞、解锁、owner、状态流转）。
13. s13 Background：引入运行槽位（runtime slot）与异步 worker；任务目标和运行实例分离。
14. s14 Scheduler：定时触发同一任务执行面（日报/周报/监控巡检）。
15. s15 Agent Teams：把角色实体化为长期队友（有身份、有生命周期、有 inbox），不再只是一次性阶段函数。
16. s16 Protocol：队友间走结构化协议（request_id、type、payload、status），替代自由文本协作。
17. s17 Autonomous：空闲队友可按角色策略自主认领任务（claim predicate + 安全边界）。
18. s18 Isolation：任务与执行车道分离；每任务独立工作区/产物区，避免并发污染。
19. s19 MCP/Plugin：外部能力总线化；本地工具与 MCP 工具进同一控制面（同路由、同权限、同审计）。