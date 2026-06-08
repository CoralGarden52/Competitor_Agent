"use client";

import type { WorkspaceReportBlock, WorkspaceReportCitation, WorkspaceReportContentItem } from "@/components/workspace-types";

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

function contentItemsOf(block: WorkspaceReportBlock): WorkspaceReportContentItem[] {
  if (!Array.isArray(block.content)) return [];
  return block.content.filter(
    (item): item is WorkspaceReportContentItem => Boolean(item) && typeof item === "object" && "text" in (item as Record<string, unknown>),
  );
}

function MatrixBlock({ block }: { block: WorkspaceReportBlock }) {
  const rows = Array.isArray(block.content) ? (block.content as Array<Record<string, unknown>>) : [];
  if (!rows.length) {
    return (
      <section className="report-block-card">
        <h2>{block.title || "二、竞品对比总览"}</h2>
        <p className="empty-state">暂无对比矩阵。</p>
      </section>
    );
  }
  const headers = Object.keys(rows[0] || {}).filter((header) => header !== "role");
  return (
    <section className="report-block-card">
      <h2>{block.title || "二、竞品对比总览"}</h2>
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
  const contentItems = contentItemsOf(block);
  if (contentItems.length) {
    return (
      <section className="report-block-card">
        <h2>{block.title || "报告章节"}</h2>
        <div className="report-block-paragraphs">
          {contentItems.map((item, index) => {
            const text = String(item.text || "").trim();
            if (!text) return null;
            const citations = Array.isArray(item.citations) ? item.citations : [];
            return (
              <div key={`${item.item_id || block.block_id}-${index}`} className="report-section-item">
                {item.kind === "bullet" ? <ul><li>{text}</li></ul> : <p>{text}</p>}
                <CitationBadges citations={citations} />
              </div>
            );
          })}
        </div>
        <CitationBadges citations={citationsOf(block)} />
      </section>
    );
  }
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

function TitleBlock({ block }: { block: WorkspaceReportBlock }) {
  return (
    <section className="report-block-card">
      <h1>{String(block.content || block.title || "竞品分析报告")}</h1>
    </section>
  );
}

function ExecutiveSummaryBlock({ block }: { block: WorkspaceReportBlock }) {
  const text = String(block.content || "").trim() || "暂无执行摘要。";
  return (
    <section className="report-block-card">
      <h2>{block.title || "执行摘要"}</h2>
      <div className="report-block-paragraphs">
        {text.split(/\n+/).filter(Boolean).map((line, index) => <p key={`${block.block_id || "summary"}-${index}`}>{line}</p>)}
      </div>
      <CitationBadges citations={citationsOf(block)} />
    </section>
  );
}

function ReferenceBlock({ block }: { block: WorkspaceReportBlock }) {
  const references = Array.isArray(block.content)
    ? block.content.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
  return (
    <section className="report-block-card report-reference-card">
      <h2>{block.title || "参考来源"}</h2>
      {references.length ? (
        <ol>
          {references.map((item, index) => <li key={`ref-${index}`}>{item}</li>)}
        </ol>
      ) : (
        <p className="empty-state">暂无参考来源。</p>
      )}
    </section>
  );
}

function blockSortWeight(block: WorkspaceReportBlock): number {
  if (block.block_type === "title") return 0;
  if (block.block_type === "executive_summary") return 1;
  if (block.section_id === "analysis_background") return 2;
  if (block.block_type === "comparison_matrix") return 3;
  if (block.section_id === "comparison_overview") return 4;
  if (block.section_id === "capability_comparison") return 5;
  if (block.section_id === "pricing_strategy") return 6;
  if (block.section_id === "user_feedback_analysis") return 7;
  if (block.section_id === "swot_analysis") return 8;
  if (block.section_id === "strategic_insights") return 9;
  if (block.section_id === "conclusion_risks") return 10;
  if (block.block_type === "reference_list") return 99;
  return 50 + Number(block.order || 0);
}

export function ReportPreviewPanel({ blocks = [], html = "", markdown = "" }: ReportPreviewPanelProps) {
  const hasMatrixBlock = blocks.some((block) => block.block_type === "comparison_matrix");
  const normalizedBlocks = [...blocks]
    .filter((block) => !(hasMatrixBlock && block.section_id === "comparison_overview"))
    .sort((left, right) => {
      const weightDiff = blockSortWeight(left) - blockSortWeight(right);
      if (weightDiff !== 0) return weightDiff;
      return Number(left.order || 0) - Number(right.order || 0);
    });

  if (normalizedBlocks.length) {
    return (
      <article className="structured-report-preview">
        <div className="structured-report-body">
          {normalizedBlocks.map((block) => {
            if (block.block_type === "title") return <TitleBlock key={block.block_id || "title"} block={block} />;
            if (block.block_type === "executive_summary") return <ExecutiveSummaryBlock key={block.block_id || "executive_summary"} block={block} />;
            if (block.block_type === "comparison_matrix") return <MatrixBlock key={block.block_id || `matrix-${block.order}`} block={block} />;
            if (block.block_type === "reference_list") return <ReferenceBlock key={block.block_id || "references"} block={block} />;
            return <SectionBlock key={block.block_id || `${block.block_type}-${block.order}`} block={block} />;
          })}
        </div>
      </article>
    );
  }

  if (html.trim()) {
    return <article className="report-preview-html" dangerouslySetInnerHTML={{ __html: html }} />;
  }

  return <pre>{markdown || "暂无报告内容"}</pre>;
}
