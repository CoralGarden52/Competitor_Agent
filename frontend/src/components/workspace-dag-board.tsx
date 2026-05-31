import type { AgentStageCard, StageName, WorkflowDag } from "@/components/workspace-types";

type WorkspaceDagBoardProps = {
  dag?: WorkflowDag;
  stages: AgentStageCard[];
  selectedStage: StageName | null;
  onSelectStage: (stage: StageName) => void;
};

const STAGE_LABELS: Record<StageName, string> = {
  plan: "Plan",
  collect: "Collect",
  normalize: "Normalize",
  analyze: "Analyze",
  draft: "Draft",
  qa: "QA",
  finalize: "Finalize",
};

function isStageName(value: string): value is StageName {
  return value in STAGE_LABELS;
}

function formatStatus(status?: string): string {
  if (status === "completed") return "完成";
  if (status === "running") return "进行中";
  if (status === "failed") return "失败";
  return "待执行";
}

export function WorkspaceDagBoard({
  dag,
  stages,
  selectedStage,
  onSelectStage,
}: WorkspaceDagBoardProps) {
  const nodes = (dag?.nodes ?? []).filter(isStageName);
  const stageMap = new Map(stages.map((item) => [item.stage, item]));

  if (!nodes.length) {
    return (
      <section className="workspace-panel workspace-dag-board" aria-label="阶段流程图">
        <div className="workspace-panel-header">
          <div>
            <p className="workspace-eyebrow">Orchestration</p>
            <h2>阶段 DAG</h2>
          </div>
        </div>
        <p className="empty-state">等待工作流结构加载...</p>
      </section>
    );
  }

  return (
    <section className="workspace-panel workspace-dag-board" aria-label="阶段流程图">
      <div className="workspace-panel-header">
        <div>
          <p className="workspace-eyebrow">Orchestration</p>
          <h2>阶段 DAG</h2>
        </div>
      </div>

      <div className="dag-scroll-shell">
        <div className="dag-track" role="list">
          {nodes.map((stage, index) => {
            const card = stageMap.get(stage);
            const status = card?.status ?? "pending";
            const isActive = selectedStage === stage;
            const showLink = index < nodes.length - 1;
            return (
              <div key={stage} className="dag-segment" role="listitem">
                <button
                  type="button"
                  className={`dag-node${isActive ? " active" : ""} ${status}`}
                  onClick={() => onSelectStage(stage)}
                  aria-pressed={isActive}
                >
                  <div className="dag-node-top">
                    <span className="dag-node-index">{index + 1}</span>
                    <span className={`status-pill ${status}`}>{formatStatus(status)}</span>
                  </div>
                  <div className="dag-node-body">
                    <strong>{STAGE_LABELS[stage]}</strong>
                    <small>{card?.agent || stage}</small>
                  </div>
                </button>
                {showLink ? (
                  <span className={`dag-arrow ${status}`} aria-hidden="true">
                    <span className="dag-arrow-line" />
                    <span className="dag-arrow-head">→</span>
                  </span>
                ) : null}
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
