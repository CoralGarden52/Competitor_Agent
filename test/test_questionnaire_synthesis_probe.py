from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"

for path in (REPO_ROOT, BACKEND_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

from app.core.agent_llm import AgentLLMClient
from app.core.config import get_config
from app.core.models import QuestionnaireDesign
from app.core.prompts.agent_prompts import QUESTIONNAIRE_MARKDOWN_SYSTEM_PROMPT


DEFAULT_SIGNALS_PATH = Path("/home/wyz/Competitor_Agent/test/questionnaire_outputs/questionnaire_signals.json")
DEFAULT_OUTPUT_DIR = Path("/home/wyz/Competitor_Agent/test/questionnaire_outputs")

QUESTIONNAIRE_BANNED_PHRASES = (
    "设计意图",
    "关联字段",
    "objective",
    "识别用户流失",
    "内部提示词",
    "schema",
    "field_refs",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run only the final questionnaire synthesis step from saved questionnaire signals.")
    parser.add_argument("--signals", type=Path, default=DEFAULT_SIGNALS_PATH, help="Path to saved questionnaire_signals JSON.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory to write synthesis outputs.")
    parser.add_argument("--debug", action="store_true", help="Print payload size and result stats.")
    args = parser.parse_args()

    source = json.loads(args.signals.read_text(encoding="utf-8"))
    questionnaire_signals = source.get("questionnaire_signals", [])
    target_audience = source.get("target_audience", "竞品相关潜在用户或现有用户")
    objective = source.get("objective", "验证竞品差异点、用户感知与转化障碍")

    payload = {
        "target_audience": target_audience,
        "objective": objective,
        "questionnaire_signals": questionnaire_signals,
        "questionnaire_requirements": {
            "sections": 4,
            "question_count_range": "12-18",
            "forbidden_phrases": list(QUESTIONNAIRE_BANNED_PHRASES),
            "must_hide_internal_notes": True,
            "must_be_user_facing": True,
        },
    }

    if args.debug:
        payload_chars = len(json.dumps(payload, ensure_ascii=False))
        print(f"[probe] signal_count={len(questionnaire_signals)}")
        print(f"[probe] payload_chars={payload_chars}")
        print(f"[probe] base_url={get_config().openai_base_url}")
        print(f"[probe] model={get_config().openai_model}")
        print(f"[probe] timeout={get_config().request_timeout_seconds}")
        print(f"[probe] retry_count={get_config().agent_llm_retry_count}")

    llm = AgentLLMClient(get_config(), store=None)
    started_at = time.time()
    markdown = llm.invoke_text(
        trace_name="test.questionnaire.synthesis_probe",
        system_prompt=QUESTIONNAIRE_MARKDOWN_SYSTEM_PROMPT,
        user_payload=payload,
        metadata={
            "run_id": "test_synthesis_probe",
            "node_name": "questionnaire",
            "agent_name": "QuestionnaireSynthesisProbe",
            "model": llm.config.openai_model,
            "probe": True,
        },
    ).strip()
    latency_ms = int((time.time() - started_at) * 1000)
    design = QuestionnaireDesign(
        title="问卷生成结果",
        target_audience=target_audience,
        objective=objective,
        introduction="",
        estimated_minutes=8,
        sections=[],
        closing_message="",
        markdown=markdown,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    md_path = args.out_dir / "questionnaire_synthesis_probe.md"
    md_path.write_text(design.markdown, encoding="utf-8")

    print(f"[probe] synthesis_ok latency_ms={latency_ms} markdown_chars={len(design.markdown)}")
    print(f"[probe] markdown_saved={md_path}")


if __name__ == "__main__":
    main()
