from __future__ import annotations

from html import escape
import logging
import re

from app.core.agent_llm import AgentLLMClient, LLMCallError
from app.core.config import get_config
from app.core.models import (
    AnalysisFieldResult,
    AnalysisSchemaField,
    CompetitorAnalysisRecord,
    DraftOutput,
    ReportBlock,
    ReportCitation,
    Report,
    ReportClaim,
    ReportSection,
    RunState,
    TaskEnvelope,
    TaskResult,
)
from app.core.prompts.agent_prompts import DRAFT_MARKDOWN_STREAM_SYSTEM_PROMPT, DRAFT_OVERVIEW_SYSTEM_PROMPT, DRAFT_SYSTEM_PROMPT


TEMPLATE_SECTION_ORDER: list[tuple[str, str, str]] = [
    ('background_goal', '一、研究范围与目标', ''),
    ('conclusion_advice', '二、核心结论', ''),
    ('comparison_overview', '三、竞品对比总览', ''),
    ('capability_comparison', '四、核心能力与产品形态', 'feature_tree'),
    ('pricing_strategy', '五、商业化与定价', 'pricing_model'),
    ('user_feedback_analysis', '六、用户反馈与采用信号', 'user_feedback'),
    ('strengths_weaknesses', '七、核心优劣势与风险', ''),
    ('action_recommendations', '八、建议动作', ''),
]

CORE_REPORT_FIELDS = {'feature_tree', 'strengths', 'weaknesses', 'pricing_model', 'user_feedback'}
SCHEMA_FIELD_ZH_LABELS = {
    'product': '产品',
    'feature_tree': '功能体系',
    'strengths': '优势',
    'weaknesses': '劣势',
    'pricing_model': '定价模式',
    'user_feedback': '用户反馈',
}
logger = logging.getLogger(__name__)


class WriterAgent:
    def __init__(self, llm: AgentLLMClient):
        self.llm = llm
        self.app_config = get_config()
        self._schema_field_zh_labels = dict(SCHEMA_FIELD_ZH_LABELS)

    def run_llm(self, state: RunState) -> DraftOutput:
        self._refresh_dynamic_schema_labels(state)
        section_specs = self._section_specs(state, include_overview_sections=False)
        payload = {
            'industry': state.industry,
            'language': state.language,
            'write_language': 'en' if str(state.language).lower().startswith('en') else 'zh',
            'analysis_schema_plan': [x.model_dump(mode='json') for x in state.analysis_schema_plan],
            'template_section_order': [{'section_id': sid, 'title': title, 'field_name': field_name} for sid, title, field_name in section_specs],
            'competitors': [x.model_dump(mode='json') for x in state.competitor_analyses],
            'profiles': [x.model_dump(mode='json') for x in state.profiles],
            'findings': [x.model_dump(mode='json') for x in state.findings],
            'evidences': [x.model_dump(mode='json') for x in state.evidences],
        }
        result = self._invoke_llm_json(
            trace_name='agent.draft.generate_report',
            system_prompt=DRAFT_SYSTEM_PROMPT,
            user_payload=payload,
            metadata={
                'run_id': state.run_id,
                'node_name': 'draft',
                'agent_name': 'WriterAgent',
                'model': self.llm.config.openai_model,
                'industry': state.industry,
                'competitor_count': len(state.effective_analysis_subject_names()),
                'attempt': state.attempt,
                'agent_name': 'WriterAgent',
                'node_name': 'draft',
            },
            tool_names=['web.extract'],
        )
        try:
            parsed = DraftOutput.model_validate(result)
            drafted = self._ensure_report_consistency(parsed, state=state, include_overview_sections=False)
            return self._synthesize_overview_sections(drafted, state=state)
        except Exception as exc:
            raise LLMCallError(
                reason='validation_error',
                message=f'DraftOutput validation failed: {exc}',
                attempt_count=self.llm.config.agent_llm_retry_count + 1,
                retry_count_used=self.llm.config.agent_llm_retry_count,
            ) from exc

    def run_markdown_stream(self, state: RunState, *, on_delta) -> str:
        self._refresh_dynamic_schema_labels(state)
        records = self._records(state)
        payload = {
            'industry': state.industry,
            'language': state.language,
            'write_language': 'en' if str(state.language).lower().startswith('en') else 'zh',
            'analysis_schema_plan': [x.model_dump(mode='json') for x in state.analysis_schema_plan],
            'template_section_order': [
                {'section_id': sid, 'title': title, 'field_name': field_name}
                for sid, title, field_name in self._section_specs(state, include_overview_sections=True)
            ],
            'competitors': [x.model_dump(mode='json') for x in state.competitor_analyses],
            'profiles': [x.model_dump(mode='json') for x in state.profiles],
            'findings': [x.model_dump(mode='json') for x in state.findings],
            'evidences': [x.model_dump(mode='json') for x in state.evidences[:24]],
            'report_requirements': {
                'must_include_matrix': True,
                'must_include_sources': True,
                'markdown_only': True,
            },
        }
        chunks: list[str] = []
        for delta in self.llm.invoke_text_stream(
            trace_name='agent.draft.generate_markdown_stream',
            system_prompt=DRAFT_MARKDOWN_STREAM_SYSTEM_PROMPT,
            user_payload=payload,
            metadata={
                'run_id': state.run_id,
                'node_name': 'draft',
                'agent_name': 'WriterAgent',
                'model': self.llm.config.openai_model,
                'industry': state.industry,
                'competitor_count': len(state.effective_analysis_subject_names()),
                'attempt': state.attempt,
            },
            temperature=0.2,
        ):
            if not delta:
                continue
            chunks.append(delta)
            on_delta(delta)
        return ''.join(chunks).strip()

    def build_streamable_report(self, state: RunState) -> DraftOutput:
        self._refresh_dynamic_schema_labels(state)
        records = self._records(state)
        report = Report(
            executive_summary=self._executive_summary_from_body(state, records, self._comparison_matrix(state, records)),
            comparison_matrix=self._comparison_matrix(state, records),
            swot=self._target_swot(state, records),
            opportunities=self._opportunity_bullets(records, state=state),
            appendix_sources=self._appendix_sources(state),
            sections=self._template_sections(state, records, include_overview_sections=False),
        )
        drafted = DraftOutput(report=report)
        drafted = self._ensure_report_consistency(drafted, state=state, include_overview_sections=False)
        drafted = self._synthesize_overview_sections(drafted, state=state, allow_llm=False)
        drafted.report.blocks = self._blocks_from_report(state, drafted.report)
        drafted.report.citations = self._global_citations_from_blocks(drafted.report.blocks)
        drafted.report.markdown = self._markdown_from_blocks(state, drafted.report)
        drafted.report.html = self._html_from_blocks(state, drafted.report)
        return drafted

    def build_report_from_markdown(self, state: RunState, markdown: str) -> DraftOutput:
        cleaned_markdown = str(markdown or '').strip()
        records = self._records(state)
        sections = self._sections_from_markdown(state, cleaned_markdown)
        report = Report(
            executive_summary=self._executive_summary_from_markdown(state, records, cleaned_markdown, sections),
            comparison_matrix=self._comparison_matrix(state, records),
            opportunities=self._opportunity_bullets(records, state=state),
            appendix_sources=self._appendix_sources(state),
            sections=sections,
            markdown=cleaned_markdown,
        )
        drafted = DraftOutput(report=report)
        drafted = self._ensure_report_consistency(drafted, state=state, include_overview_sections=True)
        drafted.report.blocks = self._blocks_from_report(state, drafted.report)
        drafted.report.citations = self._global_citations_from_blocks(drafted.report.blocks)
        drafted.report.markdown = cleaned_markdown or self._markdown_from_blocks(state, drafted.report)
        drafted.report.html = self._html_from_blocks(state, drafted.report)
        return drafted

    def run_fallback(self, state: RunState) -> DraftOutput:
        return self.build_streamable_report(state)

    def build_task_result(self, task: TaskEnvelope, drafted: DraftOutput) -> TaskResult:
        report = drafted.report
        return TaskResult(
            task_id=task.task_id,
            run_id=task.run_id,
            owner_agent='WriterAgent',
            status='completed',
            summary=f'drafted report with {len(report.sections)} sections',
            output_payload={
                'section_count': len(report.sections),
                'report_ready': bool(str(report.markdown).strip()),
            },
            changed_fields=[section.field_name for section in report.sections if section.field_name],
            next_recommendations=['finalize_run'] if bool(str(report.markdown).strip()) else ['draft_report'],
        )

    def consume_task(self, task: TaskEnvelope, state: RunState) -> tuple[TaskResult, DraftOutput]:
        try:
            drafted = self.run_llm(state)
        except LLMCallError:
            drafted = self.run_fallback(state)
        report = drafted.report
        task_result = TaskResult(
            task_id=task.task_id,
            run_id=task.run_id,
            owner_agent='WriterAgent',
            status='completed',
            summary=f'drafted report with {len(report.sections)} sections',
            output_payload={
                'section_count': len(report.sections),
                'report_ready': bool(str(report.markdown).strip()),
            },
            changed_fields=[section.field_name for section in report.sections if section.field_name],
            next_recommendations=['finalize_run'] if bool(str(report.markdown).strip()) else ['draft_report'],
        )
        return task_result, drafted

    def _sections_from_markdown(self, state: RunState, markdown: str) -> list[ReportSection]:
        specs = self._section_specs(state, include_overview_sections=True)
        if not markdown.strip():
            return []
        title_map = {title.strip(): (section_id, field_name) for section_id, title, field_name in specs}
        matched: list[tuple[str, str, str]] = []
        pattern = re.compile(r'(?m)^##\s+(.+?)\s*$')
        hits = list(pattern.finditer(markdown))
        for index, hit in enumerate(hits):
            title = hit.group(1).strip()
            start = hit.end()
            end = hits[index + 1].start() if index + 1 < len(hits) else len(markdown)
            body = markdown[start:end].strip()
            if title == '参考来源':
                continue
            spec = title_map.get(title)
            if spec is None:
                continue
            section_id, field_name = spec
            matched.append((section_id, title, body if body else '暂无内容'))
        if not matched:
            return []
        by_section_id = {section_id: (title, body) for section_id, title, body in matched}
        records = self._records(state)
        sections: list[ReportSection] = []
        for section_id, title, field_name in specs:
            entry = by_section_id.get(section_id)
            if entry is None:
                continue
            actual_title, body = entry
            claims = self._claims_and_content_for_section(
                state,
                records,
                section_id=section_id,
                title=actual_title,
                field_name=field_name,
            )[0]
            sections.append(
                ReportSection(
                    section_id=section_id,
                    title=actual_title,
                    field_name=field_name,
                    claims=claims,
                    content_markdown=body,
                )
            )
        return sections

    def _executive_summary_from_markdown(
        self,
        state: RunState,
        records: list[CompetitorAnalysisRecord],
        markdown: str,
        sections: list[ReportSection],
    ) -> str:
        for section in sections:
            if section.section_id == 'conclusion_advice':
                text = re.sub(r'(?m)^\s*[-*]\s*', '', section.content_markdown or '').strip()
                if text:
                    return text.split('\n', 1)[0][:280]
        lines = [line.strip() for line in markdown.splitlines() if line.strip() and not line.strip().startswith('#')]
        if lines:
            return lines[0][:280]
        return self._executive_summary(state, records)

    def _ensure_report_consistency(
        self,
        drafted: DraftOutput,
        *,
        state: RunState,
        include_overview_sections: bool = True,
    ) -> DraftOutput:
        report = drafted.report
        records = self._records(state)
        valid_refs = {ev.evidence_id for ev in state.evidences}
        if not report.executive_summary.strip():
            report.executive_summary = self._executive_summary(state, records)
        if not report.comparison_matrix:
            report.comparison_matrix = self._comparison_matrix(state, records)
        if not report.sections:
            report.sections = self._template_sections(state, records, include_overview_sections=include_overview_sections)
        else:
            report.sections = self._merge_into_template_sections(
                state,
                records,
                report.sections,
                include_overview_sections=include_overview_sections,
        )
        report.sections = self._normalize_report_sections(
            state,
            records,
            report.sections,
            valid_refs=valid_refs,
            include_overview_sections=include_overview_sections,
        )
        if not report.appendix_sources:
            report.appendix_sources = self._appendix_sources(state)
        if not report.opportunities:
            report.opportunities = self._opportunity_bullets(records, state=state)
        if not report.blocks:
            report.blocks = self._blocks_from_report(state, report)
        else:
            report.blocks = self._sanitize_report_blocks(report.blocks)
        if not report.citations:
            report.citations = self._global_citations_from_blocks(report.blocks)
        if not report.markdown.strip():
            report.markdown = self._markdown_from_blocks(state, report)
        if not report.html.strip():
            report.html = self._html_from_blocks(state, report)
        return DraftOutput(report=report)

    def _records(self, state: RunState) -> list[CompetitorAnalysisRecord]:
        subject_names = [name for name in state.effective_analysis_subject_names() if str(name or '').strip()]
        if state.competitor_analyses:
            record_map = {record.product_name: record for record in state.competitor_analyses}
            ordered_records = [record_map[name] for name in subject_names if name in record_map]
            seen = {record.product_name for record in ordered_records}
            for record in state.competitor_analyses:
                if record.product_name not in seen:
                    ordered_records.append(record)
                    seen.add(record.product_name)
            return ordered_records
        fallback_names = subject_names or [profile.product_name for profile in state.profiles]
        seen: set[str] = set()
        records: list[CompetitorAnalysisRecord] = []
        for name in fallback_names:
            cleaned = str(name or '').strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            records.append(CompetitorAnalysisRecord(product_name=cleaned, fields=[]))
        return records

    def _comparison_matrix(self, state: RunState, records: list[CompetitorAnalysisRecord]) -> list[dict]:
        matrix: list[dict] = []
        for record in records:
            row = {
                'product': self._display_product_name(state, record.product_name),
                'role': state.subject_role_for(record.product_name),
            }
            for field in record.fields:
                row[field.field_name] = self._format_text_for_report(field.summary, context='matrix_cell')
            matrix.append(row)
        return matrix

    def _section_specs(self, state: RunState, *, include_overview_sections: bool = True) -> list[tuple[str, str, str]]:
        specs: list[tuple[str, str, str]] = []
        dynamic_specs = [
            (f'dynamic_{item.field_name}', self._dynamic_section_title(item.field_name), item.field_name)
            for item in self._dynamic_section_fields(state)
        ]
        for section in TEMPLATE_SECTION_ORDER:
            if not include_overview_sections and section[0] in {'background_goal', 'conclusion_advice'}:
                continue
            if section[0] == 'strengths_weaknesses':
                specs.extend(dynamic_specs)
            specs.append(section)
        used_fields = {field_name for _, _, field_name in specs if field_name}
        return specs

    def _dynamic_section_fields(self, state: RunState) -> list[AnalysisSchemaField]:
        schema_plan = state.analysis_schema_plan or []
        dynamic_items = [item for item in schema_plan if item.field_name not in CORE_REPORT_FIELDS]
        dynamic_items.sort(key=lambda x: x.priority)
        return dynamic_items[:3]

    def _refresh_dynamic_schema_labels(self, state: RunState) -> None:
        field_names: list[str] = []
        seen: set[str] = set()
        for key in ['product', *CORE_REPORT_FIELDS]:
            if key not in seen:
                seen.add(key)
                field_names.append(key)
        for item in state.analysis_schema_plan or []:
            key = str(item.field_name or '').strip()
            if key and key not in seen:
                seen.add(key)
                field_names.append(key)
        for record in state.competitor_analyses or []:
            for field in record.fields:
                key = str(field.field_name or '').strip()
                if key and key not in seen:
                    seen.add(key)
                    field_names.append(key)
        for field_name in field_names:
            self._schema_field_zh_labels[field_name] = self._localize_schema_field_label(field_name)

    def _dynamic_section_title(self, field_name: str) -> str:
        label = self._schema_field_label(field_name)
        return f'动态维度：{label}'

    def _display_product_name(self, state: RunState, product_name: str) -> str:
        fit_type = self._competitor_fit_type(state, product_name)
        if fit_type == 'target':
            return f'{product_name}（目标产品）'
        if fit_type == 'direct':
            return f'{product_name}（直接竞品）'
        if fit_type == 'substitute':
            return f'{product_name}（间接竞品）'
        return product_name

    @staticmethod
    def _competitor_fit_type(state: RunState, product_name: str) -> str:
        return state.subject_role_for(product_name)

    def _template_sections(
        self,
        state: RunState,
        records: list[CompetitorAnalysisRecord],
        *,
        include_overview_sections: bool = True,
    ) -> list[ReportSection]:
        sections: list[ReportSection] = []
        for section_id, title, field_name in self._section_specs(state, include_overview_sections=include_overview_sections):
            claims, content = self._claims_and_content_for_section(state, records, section_id=section_id, title=title, field_name=field_name)
            sections.append(
                ReportSection(
                    section_id=section_id,
                    title=title,
                    field_name=field_name,
                    claims=claims,
                    content_markdown=content,
                )
            )
        return sections

    def _merge_into_template_sections(
        self,
        state: RunState,
        records: list[CompetitorAnalysisRecord],
        supplied: list[ReportSection],
        *,
        include_overview_sections: bool = True,
    ) -> list[ReportSection]:
        by_id = {item.section_id: item for item in supplied}
        merged: list[ReportSection] = []
        for section_id, title, field_name in self._section_specs(state, include_overview_sections=include_overview_sections):
            base = by_id.get(section_id)
            if base is None:
                claims, content = self._claims_and_content_for_section(state, records, section_id=section_id, title=title, field_name=field_name)
                merged.append(ReportSection(section_id=section_id, title=title, field_name=field_name, claims=claims, content_markdown=content))
                continue
            claims = base.claims or self._claims_and_content_for_section(state, records, section_id=section_id, title=title, field_name=field_name)[0]
            content = base.content_markdown.strip() or self._claims_and_content_for_section(state, records, section_id=section_id, title=title, field_name=field_name)[1]
            merged.append(
                ReportSection(
                    section_id=section_id,
                    title=base.title or title,
                    field_name=base.field_name or field_name,
                    claims=claims,
                    content_markdown=content,
                )
            )
        return merged

    def _claims_and_content_for_section(
        self,
        state: RunState,
        records: list[CompetitorAnalysisRecord],
        *,
        section_id: str,
        title: str,
        field_name: str,
    ) -> tuple[list[ReportClaim], str]:
        if section_id == 'background_goal':
            text = self._background_text(state)
            return [], text
        if section_id == 'conclusion_advice':
            claims = self._top_claims_from_records(records, limit=4)
            lines = [self._executive_summary(state, records)]
            lines.extend([f"- {claim.statement}" for claim in claims])
            return claims, '\n'.join(lines)
        if section_id == 'comparison_overview':
            claims = self._top_claims_from_records(records, limit=6)
            content = self._comparison_overview_text(records)
            return claims, content
        if section_id == 'capability_comparison':
            claims = self._field_claims(records, preferred_fields=['feature_tree'])
            return claims, self._dynamic_field_section_text(state, records, 'feature_tree') or '暂无核心能力结构证据。'
        if section_id == 'pricing_strategy':
            claims = self._field_claims(records, preferred_fields=['pricing_model'])
            content = self._dynamic_field_section_text(state, records, 'pricing_model')
            return claims, content or '暂无稳定的定价与商业化证据。'
        if section_id == 'user_feedback_analysis':
            claims = self._field_claims(records, preferred_fields=['user_feedback'])
            return claims, self._dynamic_field_section_text(state, records, 'user_feedback') or '暂无足够用户反馈证据。'
        if section_id == 'strengths_weaknesses':
            claims = self._field_claims(records, preferred_fields=['strengths', 'weaknesses'])
            return claims, self._strengths_weaknesses_text(state, records)
        if section_id == 'action_recommendations':
            claims = self._collect_weakness_claims(records)
            content = '\n'.join(f"- {item}" for item in self._opportunity_bullets(records, state=state))
            return claims, content or '暂无建议动作。'
        if field_name:
            claims = self._field_claims(records, preferred_fields=[field_name])
            content = self._dynamic_field_section_text(state, records, field_name)
            return claims, content or ('\n'.join(f"- {claim.statement}" for claim in claims) or '暂无足够字段证据。')
        claims = self._field_claims(records, preferred_fields=[])
        return claims, '\n'.join(f"- {claim.statement}" for claim in claims)

    def _blocks_from_report(self, state: RunState, report: Report) -> list[ReportBlock]:
        blocks: list[ReportBlock] = [
            ReportBlock(
                block_id='title',
                block_type='title',
                title=f"{state.target_product or '目标产品'}竞品分析报告",
                order=0,
                content=f"{state.target_product or '目标产品'}竞品分析报告",
            ),
            ReportBlock(
                block_id='executive_summary',
                block_type='executive_summary',
                title='执行摘要',
                order=1,
                content=report.executive_summary or '暂无执行摘要。',
                citations=self._citations_from_refs(state, self._claim_refs(report.sections[:2]), limit=3),
            ),
            ReportBlock(
                block_id='comparison_matrix',
                block_type='comparison_matrix',
                title='分析对象对比矩阵',
                order=2,
                content=report.comparison_matrix,
                citations=self._citations_from_refs(state, self._claim_refs(report.sections), limit=6),
            ),
        ]
        order = 3
        for section in report.sections:
            content = str(section.content_markdown or '').strip()
            block_type = 'section_bullets' if self._looks_like_bullet_section(content) else 'section_paragraph'
            blocks.append(
                ReportBlock(
                    block_id=f'section:{section.section_id}',
                    block_type=block_type,
                    section_id=section.section_id,
                    title=section.title,
                    order=order,
                    content=self._section_block_content(content, bullet_mode=block_type == 'section_bullets'),
                    citations=self._citations_from_claims(state, section.claims),
                )
            )
            order += 1
        blocks.append(
            ReportBlock(
                block_id='references',
                block_type='reference_list',
                title='参考来源',
                order=order,
                content=report.appendix_sources,
                citations=self._global_citations_from_blocks(blocks),
            )
        )
        return blocks

    def _markdown_from_blocks(self, state: RunState, report: Report) -> str:
        blocks = report.blocks or self._blocks_from_report(state, report)
        lines: list[str] = []
        for block in blocks:
            lines.extend(self._markdown_lines_for_block(block))
        return '\n'.join(lines).strip()

    def block_markdown_fragment(self, block: ReportBlock) -> str:
        return '\n'.join(self._markdown_lines_for_block(block)).strip() + '\n\n'

    def _markdown_lines_for_block(self, block: ReportBlock) -> list[str]:
        lines: list[str] = []
        default_title = '目标产品竞品分析报告'
        if block.block_type == 'title':
            lines.extend([f"# {str(block.content or default_title).strip()}", ''])
        elif block.block_type == 'executive_summary':
            lines.extend(['## 执行摘要', str(block.content or '暂无执行摘要。').strip()])
            citation_line = self._markdown_citation_line(block.citations)
            if citation_line:
                lines.append(citation_line)
            lines.append('')
        elif block.block_type == 'comparison_matrix':
            lines.extend(['## 分析对象对比矩阵', ''])
            matrix = block.content if isinstance(block.content, list) else []
            if matrix:
                headers = ['product', *[k for k in matrix[0].keys() if k not in {'product', 'role'}]]
                display_headers = [self._schema_field_label(item) for item in headers]
                lines.append('| ' + ' | '.join(display_headers) + ' |')
                lines.append('| ' + ' | '.join(['---'] * len(headers)) + ' |')
                for row in matrix:
                    lines.append('| ' + ' | '.join(str(row.get(h, '')) for h in headers) + ' |')
            else:
                lines.append('暂无对比矩阵。')
            citation_line = self._markdown_citation_line(block.citations)
            if citation_line:
                lines.append(citation_line)
            lines.append('')
        elif block.block_type in {'section_paragraph', 'section_bullets'}:
            lines.extend([f"## {block.title}", ''])
            if block.block_type == 'section_bullets':
                items = block.content if isinstance(block.content, list) else []
                lines.extend([f"- {str(item).strip()}" for item in items if str(item).strip()])
                if not items:
                    lines.append('暂无内容。')
            else:
                body = str(block.content or '').strip()
                lines.append(body or '暂无内容。')
            citation_line = self._markdown_citation_line(block.citations)
            if citation_line:
                lines.append(citation_line)
            lines.append('')
        elif block.block_type == 'reference_list':
            lines.extend(['## 参考来源', ''])
            items = block.content if isinstance(block.content, list) else []
            lines.extend([f"- {str(item).strip()}" for item in items if str(item).strip()])
        return lines

    def _html_from_blocks(self, state: RunState, report: Report) -> str:
        cards = ''.join(
            f"<div class='hero-card'><div class='hero-label'>{escape(label)}</div><div class='hero-value'>{escape(value)}</div></div>"
            for label, value in [
                ('行业', state.industry),
                ('目标产品', state.target_product or '未识别'),
                ('分析对象数量', str(len(self._records(state)))),
                ('维度数量', str(max((len(record.fields) for record in self._records(state)), default=0))),
            ]
        )
        blocks = report.blocks or self._blocks_from_report(state, report)
        hero_title = next(
            (str(block.content or '').strip() for block in blocks if block.block_type == 'title' and str(block.content or '').strip()),
            f"{state.target_product or '目标产品'}竞品分析报告",
        )
        summary_block = next((block for block in blocks if block.block_type == 'executive_summary'), None)
        sections_html = ''.join(self._report_block_html(block) for block in blocks if block.block_type in {'comparison_matrix', 'section_paragraph', 'section_bullets'})
        sources_block = next((block for block in blocks if block.block_type == 'reference_list'), None)
        sources = sources_block.content if isinstance(getattr(sources_block, 'content', None), list) else report.appendix_sources
        sources_html = ''.join(f"<li>{escape(str(url))}</li>" for url in sources if str(url).strip())
        return f"""
<div class="competitor-report">
  <style>
    .competitor-report {{
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      color: #1f2937;
      background:
        radial-gradient(circle at top right, rgba(254, 212, 164, 0.35), transparent 28%),
        linear-gradient(180deg, #fffdf9 0%, #f7f8fb 100%);
      padding: 32px;
      line-height: 1.7;
    }}
    .report-shell {{ max-width: 1080px; margin: 0 auto; }}
    .hero {{
      background: linear-gradient(135deg, #fff5eb 0%, #eef4ff 100%);
      border: 1px solid #f2d0a7;
      border-radius: 24px;
      padding: 28px;
      box-shadow: 0 18px 50px rgba(31, 41, 55, 0.08);
    }}
    .hero h1 {{ margin: 0 0 10px; font-size: 34px; }}
    .hero p {{ margin: 0; color: #4b5563; font-size: 16px; }}
    .hero-grid {{
      display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 14px; margin-top: 22px;
    }}
    .hero-card {{
      background: rgba(255,255,255,0.85); border-radius: 16px; padding: 16px 18px; border: 1px solid #e7eaf0;
    }}
    .hero-label {{ font-size: 12px; color: #6b7280; text-transform: uppercase; letter-spacing: .08em; }}
    .hero-value {{ font-size: 24px; font-weight: 700; margin-top: 6px; }}
    .matrix {{
      margin: 26px 0; background: white; border-radius: 20px; padding: 22px; border: 1px solid #e8ecf3;
      box-shadow: 0 12px 30px rgba(15, 23, 42, 0.05);
      overflow-x: auto;
    }}
    .matrix table {{ width: 100%; border-collapse: collapse; min-width: 720px; }}
    .matrix th {{
      text-align: left; background: #f3f6fb; color: #334155; font-size: 13px; padding: 12px; border-bottom: 1px solid #dbe3ee;
    }}
    .matrix td {{ padding: 12px; border-bottom: 1px solid #eef2f7; vertical-align: top; }}
    .report-section {{
      background: white; border-radius: 20px; padding: 24px; margin: 18px 0; border: 1px solid #e8ecf3;
      box-shadow: 0 12px 30px rgba(15, 23, 42, 0.05);
    }}
    .report-section h2 {{
      margin: 0 0 14px; font-size: 24px; border-left: 5px solid #7099f7; padding-left: 12px;
    }}
    .section-body p, .section-body li {{ color: #374151; }}
    .citation-badges {{
      margin-top: 14px; display: flex; flex-wrap: wrap; gap: 8px;
    }}
    .citation-badge {{
      display: inline-flex; align-items: center; padding: 4px 10px; border-radius: 999px;
      background: #eef4ff; color: #29538f; text-decoration: none; font-size: 12px; border: 1px solid #d8e4fb;
    }}
    .footer-block {{
      margin-top: 24px; background: #101828; color: white; padding: 24px; border-radius: 20px;
    }}
    .footer-block h3 {{ margin-top: 0; }}
    .footer-block ul {{ margin-bottom: 0; }}
  </style>
  <div class="report-shell">
    <div class="hero">
      <h1>{escape(hero_title)}</h1>
      <p>{escape(str(summary_block.content if summary_block is not None else report.executive_summary))}</p>
      {self._citation_badges_html(summary_block.citations if summary_block is not None else [])}
      <div class="hero-grid">{cards}</div>
    </div>
    {sections_html}
    <div class="footer-block">
      <h3>参考来源</h3>
      <ul>{sources_html or '<li>暂无来源。</li>'}</ul>
    </div>
  </div>
</div>
""".strip()

    @staticmethod
    def _looks_like_bullet_section(content: str) -> bool:
        lines = [line.strip() for line in WriterAgent._clean_report_lines(str(content or '')) if line.strip()]
        return bool(lines) and all(line.startswith('- ') for line in lines)

    @staticmethod
    def _section_block_content(content: str, *, bullet_mode: bool) -> str | list[str]:
        lines = [line.strip() for line in WriterAgent._clean_report_lines(str(content or '')) if line.strip()]
        if bullet_mode:
            return [line[2:].strip() for line in lines if line.startswith('- ')]
        return '\n'.join(lines)

    @classmethod
    def _sanitize_report_blocks(cls, blocks: list[ReportBlock]) -> list[ReportBlock]:
        sanitized: list[ReportBlock] = []
        for block in blocks:
            payload = block.model_dump(mode='json')
            block_type = str(payload.get('block_type', '') or '')
            content = payload.get('content')
            if block_type == 'section_bullets' and isinstance(content, list):
                cleaned_items = []
                for item in content:
                    text = str(item or '').strip()
                    if not text:
                        continue
                    if cls._is_provenance_line(text):
                        continue
                    cleaned_items.append(text)
                payload['content'] = cleaned_items
            elif block_type == 'section_paragraph':
                payload['content'] = '\n'.join(cls._clean_report_lines(str(content or '')))
            sanitized.append(ReportBlock.model_validate(payload))
        return sanitized

    def _citations_from_claims(self, state: RunState, claims: list[ReportClaim], *, limit: int = 3) -> list[ReportCitation]:
        refs: list[str] = []
        seen: set[str] = set()
        for claim in claims:
            for ref in claim.evidence_refs:
                if ref in seen:
                    continue
                seen.add(ref)
                refs.append(ref)
        return self._citations_from_refs(state, refs, limit=limit)

    def _citations_from_refs(self, state: RunState, refs: list[str], *, limit: int = 3) -> list[ReportCitation]:
        output: list[ReportCitation] = []
        seen_urls: set[str] = set()
        for index, ref in enumerate(refs, start=1):
            evidence = next((ev for ev in state.evidences if ev.evidence_id == ref), None)
            if evidence is None:
                continue
            url = str(evidence.source_url or '').strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            output.append(
                ReportCitation(
                    citation_id=f'citation:{ref}:{index}',
                    label=self._source_link_label(evidence.title, url, len(output) + 1),
                    url=url,
                    evidence_refs=[ref],
                    source_title=str(evidence.title or '').strip(),
                )
            )
            if len(output) >= limit:
                break
        return output

    @staticmethod
    def _claim_refs(sections: list[ReportSection]) -> list[str]:
        refs: list[str] = []
        seen: set[str] = set()
        for section in sections:
            for claim in section.claims:
                for ref in claim.evidence_refs:
                    if ref in seen:
                        continue
                    seen.add(ref)
                    refs.append(ref)
        return refs

    def _global_citations_from_blocks(self, blocks: list[ReportBlock]) -> list[ReportCitation]:
        output: list[ReportCitation] = []
        seen: set[str] = set()
        for block in blocks:
            for citation in block.citations:
                key = citation.url.strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                output.append(citation)
        return output

    @staticmethod
    def _markdown_citation_line(citations: list[ReportCitation]) -> str:
        if not citations:
            return ''
        return '溯源：' + '；'.join(f"[{item.label}]({item.url})" for item in citations if item.url.strip())

    def _report_block_html(self, block: ReportBlock) -> str:
        if block.block_type == 'comparison_matrix':
            return (
                "<section class='report-section'>"
                "<h2>分析对象对比矩阵</h2>"
                f"<div class='matrix'>{self._comparison_matrix_html(block.content if isinstance(block.content, list) else [])}</div>"
                f"{self._citation_badges_html(block.citations)}"
                "</section>"
            )
        if block.block_type == 'section_bullets':
            items = block.content if isinstance(block.content, list) else []
            body = '<ul>' + ''.join(f"<li>{self._render_inline_markdown_links(str(item))}</li>" for item in items) + '</ul>' if items else '<p>暂无内容。</p>'
        else:
            body = self._markdownish_to_html(str(block.content or ''))
        return (
            "<section class='report-section'>"
            f"<h2>{escape(block.title or '报告章节')}</h2>"
            f"<div class='section-body'>{body}</div>"
            f"{self._citation_badges_html(block.citations)}"
            "</section>"
        )

    @staticmethod
    def _citation_badges_html(citations: list[ReportCitation]) -> str:
        if not citations:
            return ''
        items = ''.join(
            f"<a class='citation-badge' href=\"{escape(item.url, quote=True)}\" target=\"_blank\" rel=\"noopener noreferrer\">{escape(item.label)}</a>"
            for item in citations
            if item.url.strip()
        )
        if not items:
            return ''
        return f"<div class='citation-badges'>{items}</div>"

    def _comparison_matrix_html(self, matrix: list[dict]) -> str:
        if not matrix:
            return '<p>暂无对比矩阵。</p>'
        headers = ['product', *[k for k in matrix[0].keys() if k != 'product']]
        head = ''.join(f'<th>{escape(self._schema_field_label(h))}</th>' for h in headers)
        rows = []
        for row in matrix:
            rows.append('<tr>' + ''.join(f"<td>{escape(str(row.get(h, '')))}</td>" for h in headers) + '</tr>')
        return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"

    @classmethod
    def _markdownish_to_html(cls, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return '<p>暂无内容。</p>'
        html_parts: list[str] = []
        current_list: list[str] = []
        for line in lines:
            if line.startswith('- '):
                current_list.append(f"<li>{cls._render_inline_markdown_links(line[2:])}</li>")
                continue
            if current_list:
                html_parts.append('<ul>' + ''.join(current_list) + '</ul>')
                current_list = []
            html_parts.append(f"<p>{cls._render_inline_markdown_links(line)}</p>")
        if current_list:
            html_parts.append('<ul>' + ''.join(current_list) + '</ul>')
        return ''.join(html_parts)

    def _top_claims_from_records(self, records: list[CompetitorAnalysisRecord], *, limit: int) -> list[ReportClaim]:
        claims: list[ReportClaim] = []
        for record in records:
            for field in record.fields:
                if field.summary.strip().lower() == 'unknown':
                    continue
                claims.append(
                    ReportClaim(
                        statement=f"{record.product_name} 在 {self._schema_field_label(field.field_name)} 维度表现：{field.summary}",
                        evidence_refs=field.evidence_refs[:3],
                        confidence=field.confidence,
                    )
                )
        claims.sort(key=lambda x: x.confidence, reverse=True)
        return claims[:limit]

    def _collect_strength_claims(self, records: list[CompetitorAnalysisRecord]) -> list[ReportClaim]:
        claims = self._field_claims(records, preferred_fields=['strengths', 'feature_tree'])
        return claims[:6]

    def _collect_weakness_claims(self, records: list[CompetitorAnalysisRecord]) -> list[ReportClaim]:
        claims = self._field_claims(records, preferred_fields=['weaknesses', 'user_feedback'])
        return claims[:6]

    def _field_claims(self, records: list[CompetitorAnalysisRecord], *, preferred_fields: list[str]) -> list[ReportClaim]:
        claims: list[ReportClaim] = []
        for record in records:
            for field_name in preferred_fields:
                field = self._get_field(record, field_name)
                if field is None or field.summary.strip().lower() == 'unknown':
                    continue
                claims.append(
                    ReportClaim(
                        statement=f"{record.product_name}: {field.summary}",
                        evidence_refs=field.evidence_refs[:3],
                        confidence=field.confidence,
                    )
                )
        return claims

    def _dynamic_field_section_text(self, state: RunState, records: list[CompetitorAnalysisRecord], field_name: str) -> str:
        lines: list[str] = []
        for record in records:
            field = self._get_field(record, field_name)
            if field is None or not self._field_has_reportable_content(field):
                continue
            primary_text = self._field_primary_text(field)
            lines.append(f"- {record.product_name}: {primary_text}")
            lines.extend(self._normalized_value_bullets(field))
        return '\n'.join(lines)

    @staticmethod
    def _normalized_value_bullets(field: AnalysisFieldResult) -> list[str]:
        payload = field.normalized_value if isinstance(field.normalized_value, dict) else {}
        bullets: list[str] = []
        if 'items' in payload and isinstance(payload.get('items'), list):
            bullets.extend([f"  - {str(item).strip()}" for item in payload.get('items', []) if str(item).strip()][:4])
        if 'key_observations' in payload and isinstance(payload.get('key_observations'), list):
            bullets.extend([f"  - {str(item).strip()}" for item in payload.get('key_observations', []) if str(item).strip()][:4])
        if 'nodes' in payload and isinstance(payload.get('nodes'), list):
            for item in payload.get('nodes', [])[:4]:
                if isinstance(item, dict):
                    name = str(item.get('name', '')).strip()
                    capability = str(item.get('capability', '')).strip()
                    if name or capability:
                        bullets.append(f"  - {name}: {capability}".strip())
        if 'tiers' in payload and isinstance(payload.get('tiers'), list):
            for item in payload.get('tiers', [])[:3]:
                if isinstance(item, dict):
                    bullets.append(
                        f"  - 套餐 {str(item.get('name', '')).strip() or 'Observed Plan'} / "
                        f"{str(item.get('price_range', 'unknown')).strip() or 'unknown'} / "
                        f"{str(item.get('billing_cycle', 'unknown')).strip() or 'unknown'}"
                    )
        if 'value' in payload and str(payload.get('value', '')).strip():
            bullets.append(f"  - 最终结论：{str(payload.get('value', '')).strip()}")
        return bullets[:6]

    def _normalize_report_sections(
        self,
        state: RunState,
        records: list[CompetitorAnalysisRecord],
        sections: list[ReportSection],
        *,
        valid_refs: set[str],
        include_overview_sections: bool = True,
    ) -> list[ReportSection]:
        spec_map = {
            section_id: (title, field_name)
            for section_id, title, field_name in self._section_specs(state, include_overview_sections=include_overview_sections)
        }
        valid_fields = {item.field_name for item in state.analysis_schema_plan}
        normalized: list[ReportSection] = []
        for section in sections:
            fallback_title, fallback_field = spec_map.get(section.section_id, (section.title, section.field_name))
            field_name = section.field_name if (not section.field_name or section.field_name in valid_fields) else fallback_field
            claims = self._normalize_claims(section.claims, valid_refs=valid_refs)
            if not claims and field_name:
                claims = self._field_claims(records, preferred_fields=[field_name])
            content = section.content_markdown.strip()
            if not content:
                content = self._claims_and_content_for_section(
                    state,
                    records,
                    section_id=section.section_id,
                    title=fallback_title,
                    field_name=field_name,
                )[1]
            normalized.append(
                ReportSection(
                    section_id=section.section_id,
                    title=section.title or fallback_title,
                    field_name=field_name,
                    claims=claims,
                    content_markdown=content,
                )
            )
        return normalized

    @staticmethod
    def _normalize_claims(claims: list[ReportClaim], *, valid_refs: set[str]) -> list[ReportClaim]:
        normalized: list[ReportClaim] = []
        for claim in claims:
            statement = claim.statement.strip()
            if not statement or statement.lower() == 'unknown':
                continue
            refs = [ref for ref in claim.evidence_refs if ref in valid_refs][:3]
            if valid_refs and not refs:
                continue
            normalized.append(ReportClaim(statement=statement, evidence_refs=refs, confidence=claim.confidence))
        return normalized

    def _tabular_bullets(self, header: str, claims: list[ReportClaim], *, positive: bool) -> str:
        lines = [header]
        if not claims:
            lines.append('暂无足够证据。')
            return '\n'.join(lines)
        for claim in claims:
            angle = '产品/功能' if positive else '产品/机会'
            tail = '启发点' if positive else '机会点'
            lines.append(f"- {angle} | {claim.statement} | 可作为我方{tail}参考")
        return '\n'.join(lines)

    def _overview_text(self, records: list[CompetitorAnalysisRecord]) -> str:
        lines = [
            '网页端：从公开页面与帮助文档观察其核心能力与使用入口。',
            '电脑客户端：若未采集到专门客户端证据，则默认记录为“需进一步确认”。',
            '移动端：结合公开产品页和用户反馈判断移动场景支持情况。',
            '其他：可补充浏览器插件、API、桌面代理等形态。',
        ]
        for record in records:
            feature = self._get_field(record, 'feature_tree')
            if feature is not None and feature.summary.strip().lower() != 'unknown':
                lines.append(f"- {record.product_name}: {feature.summary}")
        return '\n'.join(lines)

    def _comparison_overview_text(self, records: list[CompetitorAnalysisRecord]) -> str:
        lines: list[str] = []
        for record in records:
            positioning = self._positioning_summary(record)
            pricing = self._get_field(record, 'pricing_model')
            price_text = pricing.summary if pricing is not None and pricing.summary.strip().lower() != 'unknown' else '定价模式待进一步确认'
            prefix = record.product_name
            lines.append(
                f"- {prefix}：定位上 "
                f"{self._format_text_for_report(positioning, context='comparison_overview')}；商业化上 "
                f"{self._format_text_for_report(price_text, context='comparison_overview')}"
            )
        return '\n'.join(lines) or '暂无分析对象总览信息。'

    def _background_text(self, state: RunState) -> str:
        target_name = state.target_subject_name() or state.target_product or '目标产品'
        if state.user_prompt.strip():
            return f"本次竞品分析围绕用户请求展开：{state.user_prompt.strip()}。本报告以 {target_name} 为核心主体，在公开信息范围内识别目标产品与竞品在核心功能、商业策略、用户反馈和扩展维度上的差异。"
        return f"本次分析聚焦 {state.industry} 行业，基于公开网页证据对 {target_name} 及其主要竞品进行结构化对比，输出适合汇报阅读的竞品分析报告。"

    def _positioning_summary(self, record: CompetitorAnalysisRecord) -> str:
        strengths = self._get_field(record, 'strengths')
        feature = self._get_field(record, 'feature_tree')
        if strengths is not None and strengths.summary.strip().lower() != 'unknown':
            return strengths.summary
        if feature is not None and feature.summary.strip().lower() != 'unknown':
            return feature.summary
        return '公开定位信息不足，建议补充官网首页和产品介绍页。'

    def _market_positioning_summary(self, record: CompetitorAnalysisRecord) -> str:
        pricing = self._get_field(record, 'pricing_model')
        feedback = self._get_field(record, 'user_feedback')
        parts: list[str] = []
        if pricing is not None and pricing.summary.strip().lower() != 'unknown':
            parts.append(pricing.summary)
        if feedback is not None and feedback.summary.strip().lower() != 'unknown':
            parts.append(feedback.summary)
        return '；'.join(parts) if parts else '缺少稳定的用户与市场定位证据。'

    def _opportunity_bullets(self, records: list[CompetitorAnalysisRecord], *, state: RunState) -> list[str]:
        target = self._target_record(state, records)
        peers = self._peer_records(state, records)
        bullets: list[str] = []
        if target is not None:
            weakness = self._get_field(target, 'weaknesses')
            if weakness is not None and weakness.summary.strip().lower() != 'unknown':
                bullets.append(
                    f"优先补齐 {target.product_name} 的短板能力："
                    f"{self._format_text_for_report(weakness.summary, context='opportunity')}"
                )
            target_gaps = [field.field_name for field in target.fields if field.evidence_gaps]
            if target_gaps:
                gap_labels = [self._schema_field_label(item) for item in target_gaps[:3]]
                bullets.append(f"优先补充 {target.product_name} 在 {', '.join(gap_labels)} 维度的公开证据，避免核心判断失真。")
        for record in peers[:2]:
            strength = self._get_field(record, 'strengths')
            if strength is not None and strength.summary.strip().lower() != 'unknown':
                bullets.append(
                    f"针对 {record.product_name} 的优势建立应对动作："
                    f"{self._format_text_for_report(strength.summary, context='opportunity')}"
                )
        return bullets[:6] or ['优先补充目标产品的定位、商业策略和增长数据相关证据。']

    @staticmethod
    def _executive_summary(state: RunState, records: list[CompetitorAnalysisRecord]) -> str:
        if not records:
            return f'{state.industry} 目标产品竞品报告基于公开信息生成，但当前缺少稳定字段级分析结果。'
        target_name = state.target_subject_name() or records[0].product_name
        peer_count = max(len(records) - 1, 0)
        field_count = max((len(record.fields) for record in records), default=0)
        return (
            f'本报告以 {target_name} 为核心主体，对 {peer_count} 个竞品进行横向比较，'
            f'覆盖 {field_count} 个分析维度，适合用于产品判断、竞争复盘与策略讨论。'
        )

    @staticmethod
    def _appendix_sources(state: RunState) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for ev in state.evidences:
            url = ev.source_url.strip()
            if not url or url in seen:
                continue
            seen.add(url)
            urls.append(url)
        return urls

    def _strengths_weaknesses_text(self, state: RunState, records: list[CompetitorAnalysisRecord]) -> str:
        lines: list[str] = []
        target_name = state.target_subject_name()
        for record in records:
            strengths = self._get_field(record, 'strengths')
            weaknesses = self._get_field(record, 'weaknesses')
            strength_text = strengths.summary if strengths is not None and strengths.summary.strip().lower() != 'unknown' else '暂无稳定优势结论'
            weakness_text = weaknesses.summary if weaknesses is not None and weaknesses.summary.strip().lower() != 'unknown' else '暂无明确短板证据'
            prefix = f"{record.product_name}（目标产品）" if target_name and record.product_name == target_name else record.product_name
            lines.append(f"- {prefix}")
            lines.append(f"  - 优势：{self._format_text_for_report(strength_text, context='strength_weakness')}")
            lines.append(f"  - 劣势/风险：{self._format_text_for_report(weakness_text, context='strength_weakness')}")
        return '\n'.join(lines) or '暂无优劣势对比内容。'

    def _target_record(self, state: RunState, records: list[CompetitorAnalysisRecord]) -> CompetitorAnalysisRecord | None:
        target_name = state.target_subject_name()
        if target_name:
            for record in records:
                if record.product_name == target_name:
                    return record
        return records[0] if records else None

    def _peer_records(self, state: RunState, records: list[CompetitorAnalysisRecord]) -> list[CompetitorAnalysisRecord]:
        target = self._target_record(state, records)
        if target is None:
            return records[1:] if len(records) > 1 else []
        return [record for record in records if record.product_name != target.product_name]

    def _target_swot(self, state: RunState, records: list[CompetitorAnalysisRecord]) -> dict[str, list[str]]:
        target = self._target_record(state, records)
        peers = self._peer_records(state, records)
        if target is None:
            return {'strengths': [], 'weaknesses': [], 'opportunities': [], 'threats': []}

        strengths_field = self._get_field(target, 'strengths')
        weaknesses_field = self._get_field(target, 'weaknesses')
        strengths = []
        weaknesses = []
        opportunities = []
        threats = []

        if strengths_field is not None and strengths_field.summary.strip().lower() != 'unknown':
            strengths.append(self._format_text_for_report(strengths_field.summary, context='strength_weakness'))
        else:
            strengths.append(f'{target.product_name} 已形成基础产品能力，但仍需更多公开证据支撑差异化判断。')

        if weaknesses_field is not None and weaknesses_field.summary.strip().lower() != 'unknown':
            weaknesses.append(self._format_text_for_report(weaknesses_field.summary, context='strength_weakness'))
        else:
            weaknesses.append(f'{target.product_name} 当前公开资料对短板暴露有限，需要结合更多市场与用户证据验证风险。')

        peer_names = '、'.join(record.product_name for record in peers[:3]) or '主要竞品'
        opportunities.append(f'可围绕 {peer_names} 已验证的需求热点，强化 {target.product_name} 的差异化定位与商业化表达。')
        threats.append(f'{peer_names} 的公开能力与市场信号更丰富，可能在用户认知和采购决策中对 {target.product_name} 形成压力。')
        return {
            'strengths': strengths[:3],
            'weaknesses': weaknesses[:3],
            'opportunities': opportunities[:3],
            'threats': threats[:3],
        }

    @staticmethod
    def _field_has_reportable_content(field: AnalysisFieldResult) -> bool:
        summary = str(field.summary or '').strip()
        if summary and summary.lower() != 'unknown':
            return True
        return WriterAgent._normalized_value_has_signal(field.normalized_value)

    @staticmethod
    def _normalized_value_has_signal(payload: dict) -> bool:
        if not isinstance(payload, dict) or not payload:
            return False
        for value in payload.values():
            if isinstance(value, str) and value.strip() and value.strip().lower() != 'unknown':
                return True
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        return True
                    if isinstance(item, dict) and any(str(v).strip() for v in item.values() if str(v).strip() and str(v).strip().lower() != 'unknown'):
                        return True
            if isinstance(value, dict):
                if any(str(v).strip() for v in value.values() if str(v).strip() and str(v).strip().lower() != 'unknown'):
                    return True
            if isinstance(value, bool) and value:
                return True
        return False

    def _field_primary_text(self, field: AnalysisFieldResult) -> str:
        summary = str(field.summary or '').strip()
        if summary and summary.lower() != 'unknown':
            return summary
        return self._normalized_value_summary(field.field_name, field.normalized_value)

    @staticmethod
    def _normalized_value_summary(field_name: str, payload: dict) -> str:
        data = payload if isinstance(payload, dict) else {}
        if field_name == 'feature_tree':
            nodes = data.get('nodes', [])
            if isinstance(nodes, list):
                labels = []
                for item in nodes[:4]:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get('name', '')).strip()
                    capability = str(item.get('capability', '')).strip()
                    text = '：'.join(part for part in [name, capability] if part)
                    if text:
                        labels.append(text)
                if labels:
                    return '核心能力包括' + '；'.join(labels) + '。'
        if field_name == 'pricing_model':
            parts: list[str] = []
            model_type = str(data.get('model_type', '')).strip()
            if model_type and model_type.lower() != 'unknown':
                parts.append(f'定价模式为 {model_type}')
            if data.get('free_tier', False):
                parts.append('存在免费层')
            tiers = data.get('tiers', [])
            if isinstance(tiers, list) and tiers:
                tier_names = [str(item.get('name', '')).strip() for item in tiers[:3] if isinstance(item, dict) and str(item.get('name', '')).strip()]
                if tier_names:
                    parts.append('可观察到的套餐包括 ' + '、'.join(tier_names))
            if parts:
                return '；'.join(parts) + '。'
        if field_name == 'user_feedback':
            positives = data.get('positive_themes', [])
            negatives = data.get('negative_themes', [])
            parts = []
            if isinstance(positives, list):
                labels = [str(item).strip() for item in positives[:3] if str(item).strip()]
                if labels:
                    parts.append('正向反馈集中在 ' + '、'.join(labels))
            if isinstance(negatives, list):
                labels = [str(item).strip() for item in negatives[:3] if str(item).strip()]
                if labels:
                    parts.append('负向反馈集中在 ' + '、'.join(labels))
            if parts:
                return '；'.join(parts) + '。'
        if field_name in {'strengths', 'weaknesses'}:
            items = data.get('items', [])
            if isinstance(items, list):
                labels = [str(item).strip() for item in items[:4] if str(item).strip()]
                if labels:
                    return '；'.join(labels) + '。'
        if isinstance(data.get('value'), str) and str(data.get('value')).strip() and str(data.get('value')).strip().lower() != 'unknown':
            return str(data.get('value')).strip()
        observations = data.get('key_observations', [])
        if isinstance(observations, list):
            labels = [str(item).strip() for item in observations[:4] if str(item).strip()]
            if labels:
                return '；'.join(labels) + '。'
        return '已采集到部分结构化信息。'

    def _synthesize_overview_sections(self, drafted: DraftOutput, *, state: RunState, allow_llm: bool = True) -> DraftOutput:
        report = drafted.report
        payload = {
            'industry': state.industry,
            'language': state.language,
            'user_prompt': state.user_prompt,
            'task': (
                '你现在只负责生成竞品分析报告的开头两章和执行摘要。'
                '请严格基于“用户请求”和“竞品对比矩阵”来写，不要使用其他上下文，不要补充矩阵外的新事实。'
                '请输出三部分内容：'
                '1) “研究范围与目标”：说明本次分析对象、覆盖范围、重点比较维度和报告用途；'
                '2) “核心结论”：总结主要差异、适用场景分化、值得关注的竞争结论，并给出1-2条高层建议；'
                '3) “executive_summary”：写成2-3句适合放在报告开头的高密度摘要。'
                '写作风格要正式、专业、适合产品经理/业务负责人阅读。'
                '不要写参考来源、不要写 evidence_id、不要逐字段复述、不要写“本报告分析了几个竞品几个维度”这类元话术。'
            ),
            'comparison_matrix': report.comparison_matrix,
        }
        records = self._records(state)
        background_text = self._background_text_from_body(state, report.comparison_matrix)
        conclusion_text = self._conclusion_text_from_body(state, records, report.comparison_matrix)
        executive_summary = self._executive_summary_from_body(state, records, report.comparison_matrix)
        if allow_llm:
            try:
                result = self._invoke_llm_json(
                    trace_name='agent.draft.generate_overview',
                    system_prompt=DRAFT_OVERVIEW_SYSTEM_PROMPT,
                    user_payload=payload,
                    metadata={
                        'run_id': state.run_id,
                        'node_name': 'draft',
                        'agent_name': 'WriterAgent',
                        'model': self.llm.config.openai_model,
                        'industry': state.industry,
                'competitor_count': len(state.effective_analysis_subject_names()),
                        'attempt': state.attempt,
                        'stage': 'overview',
                        'agent_name': 'WriterAgent',
                        'node_name': 'draft',
                    },
                    tool_names=['web.extract'],
                )
                background_text = str(result.get('background_goal', '')).strip() or background_text
                conclusion_text = str(result.get('conclusion_advice', '')).strip() or conclusion_text
                executive_summary = str(result.get('executive_summary', '')).strip() or executive_summary
            except Exception:
                pass
        overview_sections = [
            ReportSection(section_id='background_goal', title='一、研究范围与目标', field_name='', claims=[], content_markdown=background_text),
            ReportSection(section_id='conclusion_advice', title='二、核心结论', field_name='', claims=[], content_markdown=conclusion_text),
        ]
        body_sections = [section for section in report.sections if section.section_id not in {'background_goal', 'conclusion_advice'}]
        report.sections = self._inject_overview_sections(body_sections, overview_sections, state=state)
        report.executive_summary = executive_summary
        report.blocks = self._blocks_from_report(state, report)
        report.citations = self._global_citations_from_blocks(report.blocks)
        report.markdown = self._markdown_from_blocks(state, report)
        report.html = self._html_from_blocks(state, report)
        return DraftOutput(report=report)

    def _invoke_llm_json(
        self,
        *,
        trace_name: str,
        system_prompt: str,
        user_payload: dict,
        metadata: dict,
        tool_names: list[str],
    ) -> dict:
        if hasattr(self.llm, 'invoke_json_with_tools'):
            return self.llm.invoke_json_with_tools(
                trace_name=trace_name,
                system_prompt=system_prompt,
                user_payload=user_payload,
                metadata=metadata,
                tool_names=tool_names,
            )
        return self.llm.invoke_json(
            trace_name=trace_name,
            system_prompt=system_prompt,
            user_payload=user_payload,
            metadata=metadata,
        )

    def _inject_overview_sections(
        self,
        body_sections: list[ReportSection],
        overview_sections: list[ReportSection],
        *,
        state: RunState,
    ) -> list[ReportSection]:
        body_by_id = {section.section_id: section for section in body_sections}
        overview_by_id = {section.section_id: section for section in overview_sections}
        ordered: list[ReportSection] = []
        for section_id, _, _ in self._section_specs(state, include_overview_sections=True):
            if section_id in overview_by_id:
                ordered.append(overview_by_id[section_id])
            elif section_id in body_by_id:
                ordered.append(body_by_id[section_id])
        return ordered

    def _background_text_from_body(self, state: RunState, comparison_matrix: list[dict]) -> str:
        focus_text = self._matrix_focus_text(comparison_matrix)
        target_name = state.target_subject_name() or state.target_product or '目标产品'
        if state.user_prompt.strip():
            return f"本次研究围绕“{state.user_prompt.strip()}”展开，以 {target_name} 为核心主体，选取已识别的主要竞品进行对比，重点关注{focus_text}，目标是为产品判断、方案取舍和后续策略提供一页式结论。"
        return f"本次研究聚焦 {state.industry} 方向，以 {target_name} 为核心主体，基于公开信息对目标产品与竞品的产品能力、商业模式和用户采用信号进行结构化对比，重点关注{focus_text}。"

    def _conclusion_text_from_body(
        self,
        state: RunState,
        records: list[CompetitorAnalysisRecord],
        comparison_matrix: list[dict],
    ) -> str:
        summary = self._executive_summary_from_body(state, records, comparison_matrix)
        lines = [summary]
        lines.extend(self._matrix_overview_bullets(comparison_matrix))
        actions = self._opportunity_bullets(records, state=state)[:2]
        if actions:
            lines.append('建议优先动作：')
            lines.extend(f"- {item}" for item in actions)
        return '\n'.join(lines)

    def _executive_summary_from_body(
        self,
        state: RunState,
        records: list[CompetitorAnalysisRecord],
        comparison_matrix: list[dict],
    ) -> str:
        if not records:
            return f"本次{state.industry or '竞品'}分析已形成基础报告，但当前可用于归纳的稳定字段仍然有限，建议结合正文查看已采集到的差异信息。"
        target = self._target_record(state, records)
        peers = self._peer_records(state, records)
        target_name = target.product_name if target is not None else (state.target_subject_name() or records[0].product_name)
        peer_text = '、'.join(record.product_name for record in peers[:3]) or '主要竞品'
        focus_text = self._matrix_focus_text(comparison_matrix)
        if self._matrix_has_dynamic_dimensions(comparison_matrix):
            return f"从当前公开信息看，{target_name} 与 {peer_text} 的主要差异集中在{focus_text}等维度上，其中扩展能力和商业化路径最能拉开区分度。建议优先结合目标产品与竞品矩阵判断 {target_name} 的取舍方向。"
        return f"从当前公开信息看，{target_name} 与 {peer_text} 的主要差异集中在产品能力、定价方式和用户采用信号上。建议重点结合对比总览、优劣势与建议动作章节判断 {target_name} 的下一步动作。"

    def _matrix_focus_text(self, comparison_matrix: list[dict]) -> str:
        if not comparison_matrix:
            return '核心能力、商业化、用户反馈等维度'
        keys = [key for key in comparison_matrix[0].keys() if key not in {'product', 'role'}]
        labels = [self._schema_field_label(key) for key in keys[:4]]
        labels = [label for label in labels if label]
        return '、'.join(labels) if labels else '核心能力、商业化、用户反馈等维度'

    @staticmethod
    def _matrix_has_dynamic_dimensions(comparison_matrix: list[dict]) -> bool:
        if not comparison_matrix:
            return False
        core_fields = {'product', 'role', 'feature_tree', 'strengths', 'weaknesses', 'pricing_model', 'user_feedback'}
        keys = {key for key in comparison_matrix[0].keys()}
        return any(key not in core_fields for key in keys)

    def _matrix_overview_bullets(self, comparison_matrix: list[dict]) -> list[str]:
        bullets: list[str] = []
        for row in comparison_matrix[:3]:
            product = str(row.get('product', '')).strip()
            if not product:
                continue
            highlights = []
            for key, value in row.items():
                if key in {'product', 'role'}:
                    continue
                text = ' '.join(str(value or '').split())
                if text:
                    highlights.append(f"{self._schema_field_label(key)}: {self._format_text_for_report(text, context='matrix_highlight')}")
                if len(highlights) >= 2:
                    break
            if highlights:
                bullets.append(f"- {product}：{'；'.join(highlights)}")
        return bullets[:3]

    def _schema_field_label(self, field_name: str) -> str:
        key = str(field_name or '').strip()
        if not key:
            return ''
        if key in self._schema_field_zh_labels:
            return self._schema_field_zh_labels[key]
        return self._localize_schema_field_label(key)

    @staticmethod
    def _localize_schema_field_label(field_name: str) -> str:
        key = str(field_name or '').strip()
        if not key:
            return ''
        predefined = SCHEMA_FIELD_ZH_LABELS.get(key)
        if predefined:
            return predefined
        if re.search(r'[\u4e00-\u9fff]', key):
            return key.replace('_', ' ')
        return key.replace('_', ' ')

    def _field_provenance_line(self, state: RunState, field: AnalysisFieldResult) -> str:
        links = self._evidence_links_for_refs(state, field.evidence_refs)
        if not links:
            return ''
        return f"  - 溯源：{'；'.join(links)}"

    @staticmethod
    def _clean_report_lines(text: str) -> list[str]:
        lines: list[str] = []
        for raw_line in str(text or '').splitlines():
            line = raw_line.strip()
            if not line or WriterAgent._is_provenance_line(line):
                continue
            lines.append(line)
        return lines

    @staticmethod
    def _is_provenance_line(text: str) -> bool:
        normalized = str(text or '').strip()
        if not normalized:
            return False
        normalized = re.sub(r'^[\-\*\u2022]\s*', '', normalized)
        return normalized.startswith('溯源：') or normalized.startswith('来源：')

    def _evidence_links_for_refs(self, state: RunState, refs: list[str]) -> list[str]:
        link_map: list[str] = []
        seen: set[str] = set()
        for ref in refs[:4]:
            evidence = next((ev for ev in state.evidences if ev.evidence_id == ref), None)
            if evidence is None:
                continue
            url = evidence.source_url.strip()
            if not url or url in seen:
                continue
            seen.add(url)
            label = self._source_link_label(evidence.title, url, len(seen))
            link_map.append(f"[{label}]({url})")
        return link_map

    @staticmethod
    def _source_link_label(title: str, url: str, index: int) -> str:
        cleaned_title = ' '.join((title or '').split())
        if cleaned_title:
            return cleaned_title[:40]
        return f'来源{index}'

    @staticmethod
    def _render_inline_markdown_links(text: str) -> str:
        pattern = re.compile(r'\[([^\]]+)\]\((https?://[^)]+)\)')
        html_parts: list[str] = []
        last = 0
        for match in pattern.finditer(text):
            start, end = match.span()
            html_parts.append(escape(text[last:start]))
            label = escape(match.group(1))
            url = escape(match.group(2), quote=True)
            html_parts.append(f"<a href=\"{url}\" target=\"_blank\" rel=\"noopener noreferrer\">{label}</a>")
            last = end
        html_parts.append(escape(text[last:]))
        return ''.join(html_parts)

    def _format_text_for_report(self, text: str, *, context: str) -> str:
        """
        Report text formatting strategy.
        Truncation here is presentation-layer behavior, not an LLM output/token issue.
        """
        cleaned = ' '.join(str(text or '').split())
        if not self.app_config.report_truncation_enabled:
            return cleaned
        limit = self.app_config.report_truncation_limits.get(context, 160)
        if len(cleaned) <= limit:
            return cleaned
        truncated = cleaned[: limit - 1].rstrip() + '…'
        logger.debug(
            'Report text truncated context=%s original_len=%s truncated_len=%s',
            context,
            len(cleaned),
            len(truncated),
        )
        return truncated

    @staticmethod
    def _get_field(record: CompetitorAnalysisRecord, field_name: str) -> AnalysisFieldResult | None:
        return next((item for item in record.fields if item.field_name == field_name), None)

    @staticmethod
    def _refs_for_record(record: CompetitorAnalysisRecord) -> list[str]:
        refs: list[str] = []
        seen: set[str] = set()
        for field in record.fields:
            for ref in field.evidence_refs:
                if ref in seen:
                    continue
                seen.add(ref)
                refs.append(ref)
        return refs[:3]
