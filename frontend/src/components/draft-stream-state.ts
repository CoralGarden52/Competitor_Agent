import type {
  WorkspaceEvent,
  WorkspacePayload,
  WorkspaceReportBlock,
} from "@/components/workspace-types";

export const DRAFT_STREAM_WARNING_MESSAGE = "草稿流式预览中断，正在继续生成最终报告。";
export const DRAFT_STREAM_FAILURE_MESSAGE = "报告生成失败，请稍后重试。";

type DraftPreviewState = {
  hasActivity: boolean;
  isStreaming: boolean;
  markdown: string;
  error: string;
  blocks: WorkspaceReportBlock[];
};

function readPayload(event: WorkspaceEvent): Record<string, unknown> {
  return event.payload && typeof event.payload === "object" ? event.payload : {};
}

function parseBlock(payload: Record<string, unknown>): WorkspaceReportBlock | null {
  const block = payload.block;
  return block && typeof block === "object" ? (block as WorkspaceReportBlock) : null;
}

function sortBlocks(blocks: WorkspaceReportBlock[]): WorkspaceReportBlock[] {
  return [...blocks].sort((left, right) => Number(left.order || 0) - Number(right.order || 0));
}

export function deriveDraftPreviewFromEvents(
  events: WorkspaceEvent[],
  workspace?: WorkspacePayload | null,
): DraftPreviewState {
  let hasStarted = false;
  let hasCompleted = false;
  let hasFailed = false;
  let markdown = "";
  let error = "";
  const blocksById = new Map<string, WorkspaceReportBlock>();

  for (const event of events) {
    const eventType = String(event.event_type || "");
    const payload = readPayload(event);

    if (eventType === "draft_report.started" || eventType === "draft_markdown.started") {
      hasStarted = true;
      hasCompleted = false;
      hasFailed = false;
      markdown = "";
      error = "";
      blocksById.clear();
      continue;
    }
    if (eventType === "draft_report.block_delta" || eventType === "draft_report.block_completed") {
      const block = parseBlock(payload);
      if (block?.block_id) {
        hasStarted = true;
        blocksById.set(block.block_id, block);
      }
      continue;
    }
    if (eventType === "draft_markdown.delta") {
      const delta = typeof payload.delta === "string" ? payload.delta : "";
      if (delta) {
        hasStarted = true;
        markdown += delta;
      }
      continue;
    }
    if (
      eventType === "draft_report.completed"
      || eventType === "draft_markdown.completed"
      || eventType === "draft_markdown.recovered"
    ) {
      hasCompleted = true;
      hasFailed = false;
      error = "";
      continue;
    }
    if (eventType === "draft_report.failed" || eventType === "draft_markdown.failed") {
      const terminal = payload.terminal === true;
      hasFailed = terminal;
      hasCompleted = false;
      error = terminal
        ? (typeof payload.error === "string" ? payload.error : DRAFT_STREAM_FAILURE_MESSAGE)
        : (typeof payload.error === "string" ? payload.error : DRAFT_STREAM_WARNING_MESSAGE);
    }
  }

  const runStatus = String(workspace?.run?.status || "").trim();
  const finalReport = String(workspace?.report?.markdown || "").trim();
  const finalBlocks = workspace?.report?.blocks ?? [];
  if (finalReport || finalBlocks.length || runStatus === "completed") {
    hasCompleted = true;
    hasFailed = false;
    error = "";
  } else if (runStatus === "failed" && !finalReport) {
    hasCompleted = false;
    hasFailed = true;
    error = DRAFT_STREAM_FAILURE_MESSAGE;
  }

  return {
    hasActivity: hasStarted || Boolean(markdown) || hasCompleted || hasFailed || blocksById.size > 0,
    isStreaming: (hasStarted || Boolean(markdown) || blocksById.size > 0) && !hasCompleted && !hasFailed,
    markdown,
    error,
    blocks: sortBlocks(Array.from(blocksById.values())),
  };
}

export function resolveDraftStreamError(
  workspace: WorkspacePayload | null,
  fallbackError: string,
): string {
  const runStatus = String(workspace?.run?.status || "").trim();
  const finalReport = String(workspace?.report?.markdown || "").trim();
  const finalBlocks = workspace?.report?.blocks ?? [];

  if (runStatus === "completed" || finalReport || finalBlocks.length) {
    return "";
  }
  if (runStatus === "failed" && !finalReport) {
    return DRAFT_STREAM_FAILURE_MESSAGE;
  }
  return fallbackError;
}
