"use client";

import { useState } from "react";

export function HomeWorkspace() {
  const [activeMenu, setActiveMenu] = useState<"new" | "agent" | "history">("new");
  const [query, setQuery] = useState("");

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">◈</div>
          <div>
            <h2>竞品分析智能体</h2>
            <p>CompeteAI</p>
          </div>
        </div>

        <nav className="menu" aria-label="主导航">
          <button className={activeMenu === "new" ? "menu-item active" : "menu-item"} onClick={() => setActiveMenu("new")}>新对话</button>
          <button className={activeMenu === "agent" ? "menu-item active" : "menu-item"} onClick={() => setActiveMenu("agent")}>智能体协作</button>
          <button className={activeMenu === "history" ? "menu-item active" : "menu-item"} onClick={() => setActiveMenu("history")}>演示对话</button>
        </nav>
      </aside>

      <main className="main-area">
        <div className="hero-card">
          <h1>AI 驱动的竞品分析 Agent 协作系统</h1>
          <p>多智能体协同收集信息、深度分析竞品、生成结构化洞察与报告，助力更明智的决策。</p>
          <form className="query-box" onSubmit={(e) => e.preventDefault()}>
            <input
              aria-label="分析任务输入"
              placeholder="输入竞品、行业或分析任务"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <button type="submit" aria-label="提交">↑</button>
          </form>
        </div>
      </main>
    </div>
  );
}
