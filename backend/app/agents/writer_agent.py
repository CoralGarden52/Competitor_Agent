from __future__ import annotations

from html import escape

from app.core.agent_llm import AgentLLMClient, LLMCallError
from app.core.models import (
    AnalysisFieldResult,
    AnalysisSchemaField,
    CompetitorAnalysisRecord,
    DraftOutput,
    Report,
    ReportClaim,
    ReportSection,
    RunState,
)
from app.core.prompts.agent_prompts import DRAFT_OVERVIEW_SYSTEM_PROMPT, DRAFT_SYSTEM_PROMPT


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


class WriterAgent:
    def __init__(self, llm: AgentLLMClient):
        self.llm = llm

    def run_llm(self, state: RunState) -> DraftOutput:
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
        result = self.llm.invoke_json(
            trace_name='agent.draft.generate_report',
            system_prompt=DRAFT_SYSTEM_PROMPT,
            user_payload=payload,
            metadata={
                'run_id': state.run_id,
                'node_name': 'draft',
                'agent_name': 'WriterAgent',
                'model': self.llm.config.openai_model,
                'industry': state.industry,
                'competitor_count': len(state.planned_competitors or state.competitors),
                'attempt': state.attempt,
            },
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

    def run_fallback(self, state: RunState) -> DraftOutput:
        records = self._records(state)
        matrix = self._comparison_matrix(records)
        sections = self._template_sections(state, records, include_overview_sections=False)
        report = Report(
            executive_summary=self._executive_summary(state, records),
            comparison_matrix=matrix,
            swot={
                'strengths': ['字段级分析可追溯', '报告结构与模板对齐'] if records else [],
                'weaknesses': ['部分章节依赖公开网页信号，证据深度不均'] if records else [],
                'opportunities': ['优先补充关键证据不足字段', '强化产品定位与商业策略维度'] if records else [],
                'threats': ['公开来源更新频率和质量波动'] if records else [],
            },
            opportunities=self._opportunity_bullets(records),
            appendix_sources=self._appendix_sources(state),
            sections=sections,
        )
        report.markdown = self._markdown_from_template(state, report)
        report.html = self._html_from_template(state, report)
        return self._synthesize_overview_sections(DraftOutput(report=report), state=state)

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
            report.comparison_matrix = self._comparison_matrix(records)
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
            report.opportunities = self._opportunity_bullets(records)
        if not report.markdown.strip():
            report.markdown = self._markdown_from_template(state, report)
        if not report.html.strip():
            report.html = self._html_from_template(state, report)
        return DraftOutput(report=report)

    def _records(self, state: RunState) -> list[CompetitorAnalysisRecord]:
        if state.competitor_analyses:
            return state.competitor_analyses
        return [CompetitorAnalysisRecord(product_name=profile.product_name, fields=[]) for profile in state.profiles]

    def _comparison_matrix(self, records: list[CompetitorAnalysisRecord]) -> list[dict]:
        matrix: list[dict] = []
        for record in records:
            row = {'product': record.product_name}
            for field in record.fields:
                row[field.field_name] = self._compact_text(field.summary, limit=110)
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

    @staticmethod
    def _dynamic_section_title(field_name: str) -> str:
        label = field_name.replace('_', ' ').strip()
        return f'动态维度：{label}'

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
            return claims, self._dynamic_field_section_text(records, 'feature_tree') or '暂无核心能力结构证据。'
        if section_id == 'pricing_strategy':
            claims = self._field_claims(records, preferred_fields=['pricing_model'])
            return claims, self._dynamic_field_section_text(records, 'pricing_model') or '暂无稳定的定价与商业化证据。'
        if section_id == 'user_feedback_analysis':
            claims = self._field_claims(records, preferred_fields=['user_feedback'])
            return claims, self._dynamic_field_section_text(records, 'user_feedback') or '暂无足够用户反馈证据。'
        if section_id == 'strengths_weaknesses':
            claims = self._field_claims(records, preferred_fields=['strengths', 'weaknesses'])
            return claims, self._strengths_weaknesses_text(records)
        if section_id == 'action_recommendations':
            claims = self._collect_weakness_claims(records)
            content = '\n'.join(f"- {item}" for item in self._opportunity_bullets(records))
            return claims, content or '暂无建议动作。'
        if field_name:
            claims = self._field_claims(records, preferred_fields=[field_name])
            content = self._dynamic_field_section_text(records, field_name)
            return claims, content or ('\n'.join(f"- {claim.statement}" for claim in claims) or '暂无足够字段证据。')
        claims = self._field_claims(records, preferred_fields=[])
        return claims, '\n'.join(f"- {claim.statement}" for claim in claims)

    def _markdown_from_template(self, state: RunState, report: Report) -> str:
        lines = ['# 竞品分析报告', '', report.executive_summary, '']
        lines.extend(['## 竞品对比矩阵', ''])
        if report.comparison_matrix:
            headers = ['product', *[k for k in report.comparison_matrix[0].keys() if k != 'product']]
            lines.append('| ' + ' | '.join(headers) + ' |')
            lines.append('| ' + ' | '.join(['---'] * len(headers)) + ' |')
            for row in report.comparison_matrix:
                lines.append('| ' + ' | '.join(str(row.get(h, '')) for h in headers) + ' |')
        else:
            lines.append('暂无对比矩阵。')
        for section in report.sections:
            lines.extend(['', f"## {section.title}", section.content_markdown or '暂无内容'])
        if report.appendix_sources:
            lines.extend(['', '## 参考来源'])
            lines.extend([f"- {item}" for item in report.appendix_sources])
        return '\n'.join(lines)

    def _html_from_template(self, state: RunState, report: Report) -> str:
        cards = ''.join(
            f"<div class='hero-card'><div class='hero-label'>{escape(label)}</div><div class='hero-value'>{escape(value)}</div></div>"
            for label, value in [
                ('行业', state.industry),
                ('竞品数量', str(len(self._records(state)))),
                ('维度数量', str(max((len(record.fields) for record in self._records(state)), default=0))),
            ]
        )
        table_html = self._comparison_matrix_html(report.comparison_matrix)
        sections_html = ''.join(
            "<section class='report-section'>"
            f"<h2>{escape(section.title)}</h2>"
            f"<div class='section-body'>{self._markdownish_to_html(section.content_markdown)}</div>"
            f"{self._claims_html(section.claims)}"
            "</section>"
            for section in report.sections
        )
        sources_html = ''.join(f"<li>{escape(url)}</li>" for url in report.appendix_sources)
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
    .claims {{ margin-top: 14px; display: grid; gap: 10px; }}
    .claim {{
      border-left: 4px solid #fed4a4; padding: 10px 12px; background: #fffaf4; border-radius: 10px;
    }}
    .claim small {{ color: #6b7280; display: block; margin-top: 6px; }}
    .footer-block {{
      margin-top: 24px; background: #101828; color: white; padding: 24px; border-radius: 20px;
    }}
    .footer-block h3 {{ margin-top: 0; }}
    .footer-block ul {{ margin-bottom: 0; }}
  </style>
  <div class="report-shell">
    <div class="hero">
      <h1>竞品分析报告</h1>
      <p>{escape(report.executive_summary)}</p>
      <div class="hero-grid">{cards}</div>
    </div>
    <div class="matrix">
      <h2>竞品对比矩阵</h2>
      {table_html}
    </div>
    {sections_html}
    <div class="footer-block">
      <h3>参考来源</h3>
      <ul>{sources_html or '<li>暂无来源。</li>'}</ul>
    </div>
  </div>
</div>
""".strip()

    def _comparison_matrix_html(self, matrix: list[dict]) -> str:
        if not matrix:
            return '<p>暂无对比矩阵。</p>'
        headers = ['product', *[k for k in matrix[0].keys() if k != 'product']]
        head = ''.join(f'<th>{escape(h)}</th>' for h in headers)
        rows = []
        for row in matrix:
            rows.append('<tr>' + ''.join(f"<td>{escape(str(row.get(h, '')))}</td>" for h in headers) + '</tr>')
        return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"

    def _claims_html(self, claims: list[ReportClaim]) -> str:
        if not claims:
            return ''
        items = []
        for claim in claims:
            refs = ', '.join(claim.evidence_refs[:3]) if claim.evidence_refs else '无明确引用'
            items.append(
                f"<div class='claim'><div>{escape(claim.statement)}</div><small>证据引用: {escape(refs)} | 置信度: {claim.confidence:.2f}</small></div>"
            )
        return f"<div class='claims'>{''.join(items)}</div>"

    @staticmethod
    def _markdownish_to_html(text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return '<p>暂无内容。</p>'
        html_parts: list[str] = []
        current_list: list[str] = []
        for line in lines:
            if line.startswith('- '):
                current_list.append(f"<li>{escape(line[2:])}</li>")
                continue
            if current_list:
                html_parts.append('<ul>' + ''.join(current_list) + '</ul>')
                current_list = []
            html_parts.append(f"<p>{escape(line)}</p>")
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
                        statement=f"{record.product_name} 在 {field.field_name} 维度表现：{field.summary}",
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

    def _dynamic_field_section_text(self, records: list[CompetitorAnalysisRecord], field_name: str) -> str:
        lines: list[str] = []
        for record in records:
            field = self._get_field(record, field_name)
            if field is None or field.summary.strip().lower() == 'unknown':
                continue
            lines.append(f"- {record.product_name}: {field.summary}")
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
            bullets.append(f"  - 结构化结论：{str(payload.get('value', '')).strip()}")
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
            lines.append(f"- {record.product_name}：定位上 {self._compact_text(positioning, 90)}；商业化上 {self._compact_text(price_text, 90)}")
        return '\n'.join(lines) or '暂无竞品总览信息。'

    def _background_text(self, state: RunState) -> str:
        if state.user_prompt.strip():
            return f"本次竞品分析围绕用户请求展开：{state.user_prompt.strip()}。目标是在公开信息范围内，识别竞品在核心功能、商业策略、用户反馈和扩展维度上的差异。"
        return f"本次分析聚焦 {state.industry} 行业，基于公开网页证据对主要竞品进行结构化对比，输出适合汇报阅读的竞品分析报告。"

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

    def _opportunity_bullets(self, records: list[CompetitorAnalysisRecord]) -> list[str]:
        bullets: list[str] = []
        for record in records:
            weakness = self._get_field(record, 'weaknesses')
            if weakness is not None and weakness.summary.strip().lower() != 'unknown':
                bullets.append(f"围绕 {record.product_name} 的短板补位：{self._compact_text(weakness.summary, 120)}")
            gaps = [field.field_name for field in record.fields if field.evidence_gaps]
            if gaps:
                bullets.append(f"优先补充 {record.product_name} 在 {', '.join(gaps[:3])} 维度的公开证据。")
        return bullets[:6] or ['优先补充产品定位、商业策略和增长数据相关证据。']

    @staticmethod
    def _executive_summary(state: RunState, records: list[CompetitorAnalysisRecord]) -> str:
        if not records:
            return f'{state.industry} 竞品报告基于公开信息生成，但当前缺少稳定字段级分析结果。'
        field_count = max((len(record.fields) for record in records), default=0)
        return f'本报告围绕 {len(records)} 个竞品、{field_count} 个分析维度生成，按“背景-结论-定位-策略-设计-数据-反馈”的竞品分析模板重组内容，适合内部汇报与快速决策。'

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

    def _strengths_weaknesses_text(self, records: list[CompetitorAnalysisRecord]) -> str:
        lines: list[str] = []
        for record in records:
            strengths = self._get_field(record, 'strengths')
            weaknesses = self._get_field(record, 'weaknesses')
            strength_text = strengths.summary if strengths is not None and strengths.summary.strip().lower() != 'unknown' else '暂无稳定优势结论'
            weakness_text = weaknesses.summary if weaknesses is not None and weaknesses.summary.strip().lower() != 'unknown' else '暂无明确短板证据'
            lines.append(f"- {record.product_name}")
            lines.append(f"  - 优势：{self._compact_text(strength_text, 160)}")
            lines.append(f"  - 劣势/风险：{self._compact_text(weakness_text, 160)}")
        return '\n'.join(lines) or '暂无优劣势对比内容。'

    def _synthesize_overview_sections(self, drafted: DraftOutput, *, state: RunState) -> DraftOutput:
        report = drafted.report
        body_sections = [section for section in report.sections if section.section_id not in {'background_goal', 'conclusion_advice'}]
        payload = {
            'industry': state.industry,
            'language': state.language,
            'user_prompt': state.user_prompt,
            'comparison_matrix': report.comparison_matrix,
            'sections': [
                {
                    'section_id': section.section_id,
                    'title': section.title,
                    'field_name': section.field_name,
                    'content_markdown': section.content_markdown,
                }
                for section in body_sections
            ],
        }
        records = self._records(state)
        background_text = self._background_text_from_body(state, body_sections)
        conclusion_text = self._conclusion_text_from_body(state, records, body_sections)
        executive_summary = self._executive_summary_from_body(state, records, body_sections)
        try:
            result = self.llm.invoke_json(
                trace_name='agent.draft.generate_overview',
                system_prompt=DRAFT_OVERVIEW_SYSTEM_PROMPT,
                user_payload=payload,
                metadata={
                    'run_id': state.run_id,
                    'node_name': 'draft',
                    'agent_name': 'WriterAgent',
                    'model': self.llm.config.openai_model,
                    'industry': state.industry,
                    'competitor_count': len(state.planned_competitors or state.competitors),
                    'attempt': state.attempt,
                    'stage': 'overview',
                },
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
        report.sections = self._inject_overview_sections(body_sections, overview_sections, state=state)
        report.executive_summary = executive_summary
        report.markdown = self._markdown_from_template(state, report)
        report.html = self._html_from_template(state, report)
        return DraftOutput(report=report)

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

    def _background_text_from_body(self, state: RunState, sections: list[ReportSection]) -> str:
        focus_labels = [section.title.replace('三、', '').replace('四、', '').replace('五、', '').replace('六、', '').replace('七、', '').replace('八、', '') for section in sections[:4]]
        focus_labels = [label.strip() for label in focus_labels if label.strip()]
        focus_text = '、'.join(focus_labels[:4]) if focus_labels else '核心能力、商业化、用户反馈等维度'
        if state.user_prompt.strip():
            return f"本次研究围绕“{state.user_prompt.strip()}”展开，选取已识别的主要竞品进行对比，重点关注{focus_text}，目标是为产品判断、方案取舍和后续策略提供一页式结论。"
        return f"本次研究聚焦 {state.industry} 方向的主要竞品，基于公开信息对产品能力、商业模式和用户采用信号进行结构化对比，重点关注{focus_text}。"

    def _conclusion_text_from_body(
        self,
        state: RunState,
        records: list[CompetitorAnalysisRecord],
        sections: list[ReportSection],
    ) -> str:
        summary = self._executive_summary_from_body(state, records, sections)
        lines = [summary]
        overview = next((section for section in sections if section.section_id == 'comparison_overview' and section.content_markdown.strip()), None)
        if overview is not None:
            overview_lines = [line.strip() for line in overview.content_markdown.splitlines() if line.strip()][:3]
            lines.extend(overview_lines)
        actions = self._opportunity_bullets(records)[:2]
        if actions:
            lines.append('建议优先动作：')
            lines.extend(f"- {item}" for item in actions)
        return '\n'.join(lines)

    def _executive_summary_from_body(
        self,
        state: RunState,
        records: list[CompetitorAnalysisRecord],
        sections: list[ReportSection],
    ) -> str:
        if not records:
            return f"本次{state.industry or '竞品'}分析已形成基础报告，但当前可用于归纳的稳定字段仍然有限，建议结合正文查看已采集到的差异信息。"
        names = [record.product_name for record in records[:3]]
        name_text = '、'.join(names)
        dynamic_section_count = len([section for section in sections if section.section_id.startswith('dynamic_')])
        if dynamic_section_count:
            return f"从当前公开信息看，{name_text} 等竞品的主要差异集中在产品形态、商业化路径与若干关键扩展能力上，报告正文已优先展开这些可区分竞品的维度。建议重点结合对比总览、优劣势与建议动作章节判断取舍。"
        return f"从当前公开信息看，{name_text} 等竞品的主要差异集中在产品能力、定价方式和用户采用信号上。建议重点结合对比总览、优劣势与建议动作章节判断取舍。"

    @staticmethod
    def _compact_text(text: str, limit: int) -> str:
        cleaned = ' '.join(str(text or '').split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 1].rstrip() + '…'

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
