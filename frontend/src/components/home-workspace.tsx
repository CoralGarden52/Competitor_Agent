"use client";

import { type FormEvent, useEffect, useRef, useState } from "react";

type ViewMode = "welcome" | "workspace";
type StepStatus = "pending" | "running" | "done";

type ThoughtStep = {
  id: string;
  title: string;
  status: StepStatus;
  detail?: string;
};

type SubmitResponse = {
  summary: string;
  userMessage?: string;
  thoughtSteps: ThoughtStep[];
};

type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
};

export function HomeWorkspace() {
  const [activeMenu, setActiveMenu] = useState<"new" | "agent" | "history">("new");
  const [viewMode, setViewMode] = useState<ViewMode>("welcome");
  const [query, setQuery] = useState("");
  const [taskSummary, setTaskSummary] = useState("");
  const [thoughtSteps, setThoughtSteps] = useState<ThoughtStep[]>([]);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const displaySummary = taskSummary.replace(/^任务(目标|分析)\s*[:：]\s*/u, "").trim();

  useEffect(() => {
    if (viewMode === "workspace" && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [chatMessages, viewMode]);

  function resetSession() {
    setViewMode("welcome");
    setQuery("");
    setTaskSummary("");
    setThoughtSteps([]);
    setChatMessages([]);
    setError("");
    setIsSubmitting(false);
  }

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();

    const text = query.trim();
    if (!text) {
      setError("请输入分析任务后再提交。");
      return;
    }

    setError("");
    setIsSubmitting(true);

    try {
      const response = await fetch("/api/tasks/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });

      let payload: SubmitResponse | { message?: string };
      try {
        payload = await response.json();
      } catch {
        payload = {};
      }

      if (!response.ok) {
        const message = "message" in payload ? payload.message : "";
        throw new Error(message || "提交失败，请稍后重试。");
      }

      const result = payload as SubmitResponse;
      const summary = result.summary;
      const userContent = result.userMessage?.trim() || text;

      setTaskSummary(summary);
      setThoughtSteps(Array.isArray(result.thoughtSteps) ? result.thoughtSteps : []);
      setChatMessages((prev) => [
        ...prev,
        { id: `${Date.now()}-u`, role: "user", content: userContent },
        { id: `${Date.now()}-a`, role: "assistant", content: `已收到任务，我将按该目标推进分析：${summary}` },
      ]);
      setViewMode("workspace");
      setActiveMenu("history");
      setQuery("");
    } catch (submitError) {
      const message = submitError instanceof Error ? submitError.message : "提交失败，请稍后重试。";
      setError(message);
    } finally {
      setIsSubmitting(false);
    }
  }

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
          <button
            className={activeMenu === "new" ? "menu-item active" : "menu-item"}
            onClick={() => {
              setActiveMenu("new");
              resetSession();
            }}
          >
            <span className="menu-icon" aria-hidden="true">✚</span>
            <span>新对话</span>
          </button>
          <button className={activeMenu === "agent" ? "menu-item active" : "menu-item"} onClick={() => setActiveMenu("agent")}>
            <span className="menu-icon" aria-hidden="true">◉</span>
            <span>智能体协作</span>
          </button>
          <button className={activeMenu === "history" ? "menu-item active" : "menu-item"} onClick={() => setActiveMenu("history")}>
            <span className="menu-icon" aria-hidden="true">🕘</span>
            <span>演示对话</span>
          </button>
        </nav>
        {viewMode === "workspace" && displaySummary ? (
          <div className="sidebar-summary-card" aria-label="任务摘要">
            {displaySummary}
          </div>
        ) : null}
      </aside>

      <main className="main-area">
        {viewMode === "welcome" ? (
          <div className="hero-card">
            <h1>AI 驱动的竞品分析 Agent 协作系统</h1>
            <p>多智能体协同收集信息、深度分析竞品、生成结构化洞察与报告，助力更明智的决策。</p>
            {error ? <div className="error-banner" role="alert">{error}</div> : null}
            <form className="query-box" onSubmit={handleSubmit}>
              <input
                aria-label="分析任务输入"
                placeholder="输入竞品、行业或分析任务"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                disabled={isSubmitting}
              />
              <button type="submit" aria-label="提交" disabled={isSubmitting}>
                {isSubmitting ? "…" : "↑"}
              </button>
            </form>
          </div>
        ) : (
          <section className="workspace-chat-shell" aria-label="演示对话工作区">
            <header className="workspace-topbar">
              <h1>{displaySummary || "分析进行中"}</h1>
            </header>

            <div className="workspace-scroll" ref={scrollRef}>
              {error ? <div className="error-banner" role="alert">{error}</div> : null}
              <div className="workspace-conversation">
                {chatMessages.map((message) => (
                  <div key={message.id} className={`message-row ${message.role}`}>
                    <div className={`message-bubble ${message.role}`}>{message.content}</div>
                  </div>
                ))}
                <div className="workspace-lower">
                  <section className="thought-chain-panel" aria-label="智能体思考链">
                    <h2>智能体思考链</h2>
                    {thoughtSteps.length === 0 ? (
                      <p className="empty-state">等待智能体分析中...</p>
                    ) : (
                      <ol className="thought-list">
                        {thoughtSteps.map((step) => (
                          <li key={step.id} className="thought-item">
                            <div className="thought-head">
                              <span>{step.title}</span>
                              <span className={`status-pill ${step.status}`}>{step.status}</span>
                            </div>
                            {step.detail ? <p>{step.detail}</p> : null}
                          </li>
                        ))}
                      </ol>
                    )}
                  </section>
                </div>
              </div>
            </div>

            <div className="workspace-composer">
              <form className="composer-box" onSubmit={handleSubmit}>
                <input
                  aria-label="聊天输入"
                  placeholder="继续输入分析需求或追问..."
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  disabled={isSubmitting}
                />
                <button type="submit" aria-label="发送" disabled={isSubmitting}>
                  {isSubmitting ? "…" : "↑"}
                </button>
              </form>
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
