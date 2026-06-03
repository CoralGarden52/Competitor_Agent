from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

for path in (REPO_ROOT, BACKEND_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from app.agents.questionnaire_agent import QuestionnaireAgent
from app.core.agent_llm import AgentLLMClient
from app.core.config import get_config
from app.core.models import AnalysisSchemaField, Report, RunState


DEFAULT_REPORT_PATH = Path("/home/wyz/Competitor_Agent/test/run_a3e264f53b49.md")
DEFAULT_OUTPUT_DIR = Path("/home/wyz/Competitor_Agent/test/questionnaire_outputs")
DEFAULT_SIGNALS_PATH = DEFAULT_OUTPUT_DIR / "questionnaire_signals.json"


def truncate_markdown(markdown: str, *, max_lines: int) -> str:
    if max_lines <= 0:
        return markdown
    lines = markdown.splitlines()
    return "\n".join(lines[:max_lines]).strip()


def extract_competitors(markdown: str) -> list[str]:
    competitors: list[str] = []
    seen: set[str] = set()
    for line in markdown.splitlines():
        if not line.startswith("| "):
            continue
        parts = [item.strip() for item in line.strip().strip("|").split("|")]
        if not parts or parts[0] in {"产品", "---"}:
            continue
        name = re.sub(r"（.*?）", "", parts[0]).strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        competitors.append(name)
    return competitors


def extract_schema_fields(markdown: str) -> list[tuple[str, str]]:
    header_match = re.search(r"^\| 产品 \|(.+?)\|$\n^\| ---", markdown, flags=re.MULTILINE)
    if not header_match:
        return [
            ("field_01", "功能体验"),
            ("field_02", "价格方案"),
            ("field_03", "用户反馈"),
            ("field_04", "安全与合规"),
            ("field_05", "生态集成"),
        ]
    raw = header_match.group(1)
    columns = [item.strip() for item in raw.split("|") if item.strip()]
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, column in enumerate(columns, start=1):
        field_name = normalize_field_name(column, index=index)
        if field_name in seen:
            continue
        seen.add(field_name)
        output.append((field_name, column))
    return output


def normalize_field_name(value: str, *, index: int) -> str:
    token = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower())
    token = re.sub(r"_+", "_", token).strip("_")
    if token and re.search(r"[a-zA-Z]", token):
        return token
    digest = hashlib.md5(value.encode("utf-8")).hexdigest()[:6]
    return f"field_{index:02d}_{digest}"


def build_state_from_report(markdown: str) -> RunState:
    competitors = extract_competitors(markdown)
    schema_fields = extract_schema_fields(markdown)
    schema_plan = [
        AnalysisSchemaField(
            field_name=field_name,
            query_templates=[f"{{product}} {field_label}", f"{{product}} {field_label} 对比"],
            recommended_sources=["official", "public_web", "community"],
            priority=index + 1,
            corpus_refs=[f"display_label:{field_label}"],
        )
        for index, (field_name, field_label) in enumerate(schema_fields)
    ]
    report = Report(
        executive_summary=markdown.splitlines()[2].strip() if len(markdown.splitlines()) > 2 else "竞品分析报告摘要",
        markdown=markdown,
    )
    return RunState(
        industry="online_meeting",
        competitors=competitors,
        planned_competitors=competitors,
        user_prompt="基于竞品分析报告生成用户调研问卷",
        analysis_schema_plan=schema_plan,
        report=report,
        status="completed",
    )


def summarize_signal_payload(signals) -> dict[str, object]:
    serializable = [item.model_dump(mode="json") for item in signals]
    payload_json = json.dumps(serializable, ensure_ascii=False)
    per_chunk: list[dict[str, object]] = []
    total_candidate_questions = 0
    total_candidate_dimensions = 0
    total_key_points = 0
    total_user_phrases = 0
    total_decision_factors = 0
    total_risk_points = 0

    for item in serializable:
        question_count = len(item.get("candidate_questions", []))
        dimension_count = len(item.get("candidate_dimensions", []))
        key_point_count = len(item.get("key_points", []))
        user_phrase_count = len(item.get("user_phrases", []))
        decision_factor_count = len(item.get("decision_factors", []))
        risk_point_count = len(item.get("risk_points", []))
        total_candidate_questions += question_count
        total_candidate_dimensions += dimension_count
        total_key_points += key_point_count
        total_user_phrases += user_phrase_count
        total_decision_factors += decision_factor_count
        total_risk_points += risk_point_count
        per_chunk.append(
            {
                "chunk_id": item.get("chunk_id", ""),
                "chunk_title": item.get("chunk_title", ""),
                "candidate_questions": question_count,
                "candidate_dimensions": dimension_count,
                "key_points": key_point_count,
                "user_phrases": user_phrase_count,
                "decision_factors": decision_factor_count,
                "risk_points": risk_point_count,
                "json_chars": len(json.dumps(item, ensure_ascii=False)),
            }
        )

    return {
        "signal_count": len(serializable),
        "payload_chars": len(payload_json),
        "total_candidate_questions": total_candidate_questions,
        "total_candidate_dimensions": total_candidate_dimensions,
        "total_key_points": total_key_points,
        "total_user_phrases": total_user_phrases,
        "total_decision_factors": total_decision_factors,
        "total_risk_points": total_risk_points,
        "per_chunk": per_chunk,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate questionnaire design from an existing markdown report.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH, help="Path to source markdown report.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory to write questionnaire outputs.")
    parser.add_argument("--target-audience", default="竞品相关潜在用户或现有用户", help="Target audience for questionnaire design.")
    parser.add_argument("--objective", default="验证竞品差异点、用户感知与转化障碍", help="Questionnaire objective.")
    parser.add_argument("--max-lines", type=int, default=0, help="Use only the first N lines of the report. Set 0 to disable truncation.")
    parser.add_argument("--debug", action="store_true", help="Print detailed questionnaire generation logs.")
    parser.add_argument("--signals-out", type=Path, default=DEFAULT_SIGNALS_PATH, help="Path to write extracted questionnaire signals JSON.")
    args = parser.parse_args()

    full_markdown = args.report.read_text(encoding="utf-8")
    markdown = truncate_markdown(full_markdown, max_lines=args.max_lines)
    state = build_state_from_report(markdown)
    if args.debug:
        print(f"[test] full_report_chars={len(full_markdown)}")
        print(f"[test] used_report_chars={len(markdown)}")
        print(f"[test] used_report_lines={len(markdown.splitlines())}")
        print(f"[test] competitors={state.planned_competitors}")
        print(f"[test] schema_fields={[item.field_name for item in state.analysis_schema_plan]}")

    llm = AgentLLMClient(get_config(), store=None)
    agent = QuestionnaireAgent(llm)
    args.signals_out.parent.mkdir(parents=True, exist_ok=True)

    if args.debug:
        original_split = agent._split_report_for_questionnaire
        original_extract = agent._extract_signals_from_chunk
        original_parallel = agent._extract_signals_parallel
        original_run = agent.run_llm

        def debug_split(self, report_markdown: str):
            chunks = original_split(report_markdown)
            print(f"[test] questionnaire.start chunks={len(chunks)} report_chars={len(report_markdown)}")
            for chunk in chunks:
                print(f"[test] questionnaire.chunk id={chunk['chunk_id']} title={chunk['chunk_title']} chars={len(chunk['content'])}")
            return chunks

        def debug_extract(self, chunk, *, state, target_audience, objective):
            started_at = time.time()
            print(f"[test] questionnaire.signal_start id={chunk['chunk_id']} title={chunk['chunk_title']} chars={len(chunk['content'])}")
            signal = original_extract(chunk, state=state, target_audience=target_audience, objective=objective)
            latency_ms = int((time.time() - started_at) * 1000)
            print(
                f"[test] questionnaire.signal_ok id={chunk['chunk_id']} latency_ms={latency_ms} "
                f"questions={len(signal.candidate_questions)} dimensions={len(signal.candidate_dimensions)} points={len(signal.key_points)}"
            )
            return signal

        def debug_parallel(self, *, chunks, state, target_audience, objective):
            try:
                signals = original_parallel(chunks=chunks, state=state, target_audience=target_audience, objective=objective)
            except Exception as exc:
                print(f"[test] questionnaire.signal_failed error={exc}")
                raise
            print(f"[test] questionnaire.signals_extracted count={len(signals)}")
            for signal in signals:
                print(
                    f"[test] questionnaire.signal id={signal.chunk_id} title={signal.chunk_title} "
                    f"questions={len(signal.candidate_questions)} dimensions={len(signal.candidate_dimensions)} points={len(signal.key_points)}"
                )
            signals_payload = {
                "target_audience": target_audience,
                "objective": objective,
                "questionnaire_signals": [item.model_dump(mode="json") for item in signals],
            }
            args.signals_out.write_text(json.dumps(signals_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[test] questionnaire.signals_saved path={args.signals_out}")
            summary = summarize_signal_payload(signals)
            print(
                f"[test] questionnaire.synthesis_payload signal_count={summary['signal_count']} "
                f"payload_chars={summary['payload_chars']} "
                f"candidate_questions={summary['total_candidate_questions']} "
                f"dimensions={summary['total_candidate_dimensions']} "
                f"key_points={summary['total_key_points']} "
                f"user_phrases={summary['total_user_phrases']} "
                f"decision_factors={summary['total_decision_factors']} "
                f"risk_points={summary['total_risk_points']}"
            )
            for row in summary["per_chunk"]:
                print(
                    f"[test] questionnaire.synthesis_chunk id={row['chunk_id']} title={row['chunk_title']} "
                    f"json_chars={row['json_chars']} questions={row['candidate_questions']} "
                    f"dimensions={row['candidate_dimensions']} key_points={row['key_points']} "
                    f"user_phrases={row['user_phrases']} decision_factors={row['decision_factors']} "
                    f"risk_points={row['risk_points']}"
                )
            return signals

        def debug_run(self, state, *, target_audience='竞品相关潜在用户或现有用户', objective='验证竞品差异点、用户感知与转化障碍'):
            print("[test] questionnaire.run_llm start")
            started_at = time.time()
            try:
                design = original_run(state, target_audience=target_audience, objective=objective)
            except Exception as exc:
                print(f"[test] questionnaire.run_llm failed error={exc}")
                raise
            latency_ms = int((time.time() - started_at) * 1000)
            print(
                f"[test] questionnaire.run_llm ok latency_ms={latency_ms} "
                f"sections={len(design.sections)} questions={sum(len(section.questions) for section in design.sections)}"
            )
            return design

        agent._split_report_for_questionnaire = types.MethodType(debug_split, agent)
        agent._extract_signals_from_chunk = types.MethodType(debug_extract, agent)
        agent._extract_signals_parallel = types.MethodType(debug_parallel, agent)
        agent.run_llm = types.MethodType(debug_run, agent)

    if not llm.enabled():
        print("LLM is not configured. Questionnaire generation now requires a working LLM configuration.", file=sys.stderr)
        raise SystemExit(1)

    if args.debug:
        print(f"[test] base_url={llm.config.openai_base_url}")
        print(f"[test] model={llm.config.openai_model}")
        print(f"[test] timeout={llm.config.request_timeout_seconds}")
        print(f"[test] retry_count={llm.config.agent_llm_retry_count}")

    design = agent.run_llm(state, target_audience=args.target_audience, objective=args.objective)
    mode = "llm"

    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / f"questionnaire_{mode}.json"
    markdown_path = args.out_dir / f"questionnaire_{mode}.md"
    json_path.write_text(json.dumps(design.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(design.markdown, encoding="utf-8")

    print(f"mode={mode}")
    print(f"report={args.report}")
    print(f"report_lines_used={len(markdown.splitlines())}")
    print(f"json={json_path}")
    print(f"markdown={markdown_path}")
    print(f"title={design.title}")
    print(f"sections={len(design.sections)}")


if __name__ == "__main__":
    main()
