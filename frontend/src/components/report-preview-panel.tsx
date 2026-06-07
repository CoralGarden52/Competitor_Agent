"use client";

import type { WorkspaceReportBlock, WorkspaceReportCitation } from "@/components/workspace-types";

type ReportPreviewPanelProps = {
  blocks?: WorkspaceReportBlock[];
  html?: string;
  markdown?: string;
};

const FIELD_LABELS: Record<string, string> = {
  product: "产品",
  feature_tree: "功能体系",
  strengths: "优势",
  weaknesses: "劣势",
  pricing_model: "定价模式",
  user_feedback: "用户反馈",
};

function citationsOf(block: WorkspaceReportBlock): WorkspaceReportCitation[] {
  return Array.isArray(block.citations) ? block.citations : [];
}

function fieldLabel(fieldName: string): string {
  const key = String(fieldName || "").trim();
  if (!key) return "";
  if (FIELD_LABELS[key]) return FIELD_LABELS[key];
  return key.replace(/_/g, " ");
}

function CitationBadges({ citations }: { citations: WorkspaceReportCitation[] }) {
  if (!citations.length) return null;
  return (
    <div className="report-block-citations">
      {citations.map((item, index) => {
        const url = String(item.url || "").trim();
        const label = String(item.label || item.source_title || `来源${index + 1}`).trim();
        if (!url) return null;
        return (
          <a
            key={`${url}-${index}`}
            className="report-citation-badge"
            href={url}
            target="_blank"
            rel="noreferrer"
          >
            {label}
          </a>
        );
      })}
    </div>
  );
}

function MatrixBlock({ block }: { block: WorkspaceReportBlock }) {
  const rows = Array.isArray(block.content) ? (block.content as Array<Record<string, unknown>>) : [];
  if (!rows.length) {
    return (
      <section className="report-block-card">
        <h2>{block.title || "竞品对比矩阵"}</h2>
        <p className="empty-state">暂无对比矩阵。</p>
      </section>
    );
  }
  const headers = Object.keys(rows[0] || {});
  return (
    <section className="report-block-card">
      <h2>{block.title || "竞品对比矩阵"}</h2>
      <div className="report-matrix-table">
        <table>
          <thead>
            <tr>{headers.map((header) => <th key={header}>{fieldLabel(header)}</th>)}</tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={`row-${rowIndex}`}>
                {headers.map((header) => <td key={`${rowIndex}-${header}`}>{String(row[header] ?? "")}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <CitationBadges citations={citationsOf(block)} />
    </section>
  );
}

function SectionBlock({ block }: { block: WorkspaceReportBlock }) {
  const isBullets = block.block_type === "section_bullets";
  const items = Array.isArray(block.content) ? block.content.map((item) => String(item || "").trim()).filter(Boolean) : [];
  const text = typeof block.content === "string" ? block.content : "";
  return (
    <section className="report-block-card">
      <h2>{block.title || "报告章节"}</h2>
      {isBullets ? (
        items.length ? <ul>{items.map((item, index) => <li key={`${block.block_id}-${index}`}>{item}</li>)}</ul> : <p className="empty-state">暂无内容。</p>
      ) : (
        <div className="report-block-paragraphs">
          {(text || "暂无内容。").split(/\n+/).filter(Boolean).map((line, index) => <p key={`${block.block_id}-${index}`}>{line}</p>)}
        </div>
      )}
      <CitationBadges citations={citationsOf(block)} />
    </section>
  );
}

export function ReportPreviewPanel({ blocks = [], html = "", markdown = "" }: ReportPreviewPanelProps) {
  const normalizedBlocks = [...blocks].sort((left, right) => Number(left.order || 0) - Number(right.order || 0));
  if (normalizedBlocks.length) {
    const titleBlock = normalizedBlocks.find((block) => block.block_type === "title");
    const summaryBlock = normalizedBlocks.find((block) => block.block_type === "executive_summary");
    const referenceBlock = normalizedBlocks.find((block) => block.block_type === "reference_list");
    const bodyBlocks = normalizedBlocks.filter((block) => block.block_type !== "title" && block.block_type !== "executive_summary" && block.block_type !== "reference_list");
    const references = Array.isArray(referenceBlock?.content) ? referenceBlock.content.map((item) => String(item || "").trim()).filter(Boolean) : [];

    return (
      <article className="structured-report-preview">
        <header className="structured-report-hero">
          <h1>{String(titleBlock?.content || "竞品分析报告")}</h1>
          <div className="structured-report-summary">
            <p>{String(summaryBlock?.content || "暂无执行摘要。")}</p>
            <CitationBadges citations={summaryBlock ? citationsOf(summaryBlock) : []} />
          </div>
        </header>
        <div className="structured-report-body">
          {bodyBlocks.map((block) => {
            if (block.block_type === "comparison_matrix") return <MatrixBlock key={block.block_id || `matrix-${block.order}`} block={block} />;
            return <SectionBlock key={block.block_id || `${block.block_type}-${block.order}`} block={block} />;
          })}
        </div>
        {references.length ? (
          <section className="report-block-card report-reference-card">
            <h2>{referenceBlock?.title || "参考来源"}</h2>
            <ol>
              {references.map((item, index) => <li key={`ref-${index}`}>{item}</li>)}
            </ol>
          </section>
        ) : null}
      </article>
    );
  }

  if (html.trim()) {
    return <article className="report-preview-html" dangerouslySetInnerHTML={{ __html: html }} />;
  }

  return <pre>{markdown || "暂无报告内容"}</pre>;
}
