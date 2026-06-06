from __future__ import annotations

from collections import Counter

from app.core.models import QAResult, ReworkIssue, RunState, StageName
from app.core.schema_registry import get_domain_schema
from app.core.storage import PostgresStore


def validate_core_fields(state: RunState) -> list[ReworkIssue]:
    issues: list[ReworkIssue] = []
    expected_fields = [item.field_name for item in state.analysis_schema_plan]
    expected_competitors = state.planned_competitors or state.competitors
    analysis_map = {item.product_name: {field.field_name for field in item.fields} for item in state.competitor_analyses}

    for competitor in expected_competitors:
        observed = analysis_map.get(competitor, set())
        if not observed:
            issues.append(
                ReworkIssue(code='competitor_analysis.missing', message=f'{competitor} missing field-level analysis.', stage=StageName.analyze)
            )
            continue
        missing_fields = [field for field in expected_fields if field not in observed]
        if missing_fields:
            issues.append(
                ReworkIssue(
                    code='competitor_analysis.field_coverage_missing',
                    message=f'{competitor} missing analysis fields: {", ".join(missing_fields)}',
                    stage=StageName.analyze,
                )
            )

    if not state.profiles:
        issues.append(ReworkIssue(code='profiles.missing', message='No competitor profiles produced.', stage=StageName.analyze))
        return issues

    evidence_ids = {item.evidence_id for item in state.evidences}
    for profile in state.profiles:
        if not profile.feature_tree:
            issues.append(ReworkIssue(code='feature_tree.missing', message=f'{profile.product_name} missing feature_tree', stage=StageName.analyze))
        if not profile.advantages:
            issues.append(ReworkIssue(code='advantages.missing', message=f'{profile.product_name} missing advantages', stage=StageName.analyze))
        if not profile.disadvantages:
            issues.append(ReworkIssue(code='disadvantages.missing', message=f'{profile.product_name} missing disadvantages', stage=StageName.analyze))
        if not profile.pricing_model.tiers:
            issues.append(ReworkIssue(code='pricing.tiers_missing', message=f'{profile.product_name} pricing tiers missing', stage=StageName.collect))
        if not profile.user_feedback.positive_themes and not profile.user_feedback.negative_themes:
            issues.append(ReworkIssue(code='feedback.missing', message=f'{profile.product_name} user_feedback missing', stage=StageName.collect))

    for finding in state.findings:
        if not finding.evidence_refs:
            issues.append(ReworkIssue(code='finding.refs_missing', message=f'{finding.finding_id} has no evidence refs', stage=StageName.draft))
        else:
            unresolved = [ref for ref in finding.evidence_refs if ref not in evidence_ids]
            if unresolved:
                issues.append(
                    ReworkIssue(
                        code='finding.refs_invalid',
                        message=f'{finding.finding_id} has invalid evidence refs: {", ".join(unresolved)}',
                        stage=StageName.collect,
                    )
                )
    return issues


def validate_domain_extensions(state: RunState, store: PostgresStore) -> list[ReworkIssue]:
    schema = get_domain_schema(store, state.industry)
    issues: list[ReworkIssue] = []
    if not schema.required_extension_fields:
        return issues

    for profile in state.profiles:
        missing = [key for key in schema.required_extension_fields if key not in profile.domain_extensions]
        if missing:
            issues.append(
                ReworkIssue(
                    code='domain_extensions.missing',
                    message=f'{profile.product_name} missing domain fields: {", ".join(missing)}',
                    stage=StageName.analyze,
                )
            )
    return issues


def validate_self_eval(state: RunState) -> list[ReworkIssue]:
    issues: list[ReworkIssue] = []
    if not state.self_eval:
        return [ReworkIssue(code='self_eval.missing', message='No self-evaluation found.', stage=StageName.analyze)]

    for stage_name, eval_result in state.self_eval.items():
        if eval_result.coverage < 0.5:
            issues.append(
                ReworkIssue(code='self_eval.low_coverage', message=f'{stage_name} coverage below threshold', stage=StageName.collect)
            )
        if eval_result.evidence_quality < 0.5:
            issues.append(
                ReworkIssue(code='self_eval.low_evidence_quality', message=f'{stage_name} evidence quality below threshold', stage=StageName.collect)
            )
    return issues


def validate_report_structure(state: RunState) -> list[ReworkIssue]:
    if state.report is None:
        return [ReworkIssue(code='report.missing', message='No report produced.', stage=StageName.draft)]
    issues: list[ReworkIssue] = []
    if not state.report.sections:
        issues.append(ReworkIssue(code='report.sections_missing', message='Report missing structured sections.', stage=StageName.draft))
    if not state.report.appendix_sources:
        issues.append(ReworkIssue(code='report.sources_missing', message='Report missing appendix_sources.', stage=StageName.draft))
    if not state.report.html:
        issues.append(ReworkIssue(code='report.html_missing', message='Report missing html output.', stage=StageName.draft))
    return issues


def target_agent_from_issues(issues: list[ReworkIssue]) -> str:
    if not issues:
        return 'Draft'
    counter = Counter([issue.stage.value for issue in issues])
    dominant = counter.most_common(1)[0][0]
    if dominant in ('collect', 'normalize'):
        return 'Collect'
    if dominant == 'analyze':
        return 'Analyze'
    return 'Draft'


def run_qa_gate(state: RunState, store: PostgresStore) -> QAResult:
    issues = []
    issues.extend(validate_core_fields(state))
    issues.extend(validate_domain_extensions(state, store))
    issues.extend(validate_self_eval(state))
    issues.extend(validate_report_structure(state))
    if not issues:
        return QAResult(passed=True)
    return QAResult(passed=False, issues=issues, target_agent=target_agent_from_issues(issues))
