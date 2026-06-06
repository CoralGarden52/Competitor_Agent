from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.agent_llm import AgentLLMClient, LLMCallError
from app.core.models import QAOutput, RunState
from app.core.prompts.agent_prompts import (
    QA_ANALYSIS_REVIEW_SYSTEM_PROMPT,
    QA_SYSTEM_PROMPT,
)
from app.core.storage import PostgresStore


class QACriticAgent:
    _SENSITIVE_KEYS = {"api_key", "token", "secret", "password", "authorization"}
    _URL_SENSITIVE_KEYS = {"token", "sig", "signature", "key", "api_key", "access_token"}

    def __init__(self, llm: AgentLLMClient, store: PostgresStore):
        self.llm = llm
        self.store = store

    def run_llm(self, state: RunState) -> QAOutput:
        payload = {
            "industry": state.industry,
            "language": state.language,
            "analysis_schema_plan": [x.model_dump(mode="json") for x in state.analysis_schema_plan],
            "expected_competitors": state.planned_competitors or state.competitors,
            "competitors": [x.model_dump(mode="json") for x in state.competitor_analyses],
            "profiles": [x.model_dump(mode="json") for x in state.profiles],
            "findings": [x.model_dump(mode="json") for x in state.findings],
            "report": state.report.model_dump(mode="json") if state.report else None,
            "evidences": [x.model_dump(mode="json") for x in state.evidences],
            "field_evidence_summary": self._build_field_evidence_summary(state),
            "constraints": {
                "require_traceable_evidence": True,
                "default_language": "zh",
                "query_per_plan_item_min": 2,
                "query_per_plan_item_max": 4,
            },
        }
        self._append_qa_log(
            event_type="qa_input",
            run_state=state,
            payload={
                "payload_stats": self._payload_stats(payload),
                "user_payload": payload,
            },
        )
        metadata = {
            "run_id": state.run_id,
            "node_name": "qa",
            "agent_name": "QACriticAgent",
            "model": self.llm.config.openai_model,
            "industry": state.industry,
            "competitor_count": len(state.planned_competitors or state.competitors),
            "attempt": state.attempt,
        }
        try:
            result = self._invoke_llm_json(
                trace_name="agent.qa.evaluate_report",
                system_prompt=QA_SYSTEM_PROMPT,
                user_payload=payload,
                metadata=metadata,
                tool_names=["web.search", "web.fetch"],
            )
        except Exception as exc:
            self._append_qa_log(
                event_type="qa_error",
                run_state=state,
                payload={
                    "trace_name": "agent.qa.evaluate_report",
                    "payload_stats": self._payload_stats(payload),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "reason": getattr(exc, "reason", "unknown"),
                    "attempt_count": getattr(exc, "attempt_count", 0),
                    "retry_count_used": getattr(exc, "retry_count_used", 0),
                },
            )
            raise
        try:
            validated = QAOutput.model_validate(result)
            self._append_qa_log(
                event_type="qa_output",
                run_state=state,
                payload={
                    "raw_result": result,
                    "validated_output": validated.model_dump(mode="json"),
                },
            )
            return validated
        except Exception as exc:
            self._append_qa_log(
                event_type="qa_error",
                run_state=state,
                payload={
                    "trace_name": "agent.qa.evaluate_report",
                    "payload_stats": self._payload_stats(payload),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "reason": "validation_error",
                    "attempt_count": self.llm.config.agent_llm_retry_count + 1,
                    "retry_count_used": self.llm.config.agent_llm_retry_count,
                    "raw_result": result,
                },
            )
            raise LLMCallError(
                reason="validation_error",
                message=f"QAOutput validation failed: {exc}",
                attempt_count=self.llm.config.agent_llm_retry_count + 1,
                retry_count_used=self.llm.config.agent_llm_retry_count,
            ) from exc

    def run_fallback(self, state: RunState) -> QAOutput:
        # LLM-driven QA mode: do not fallback to rule-based qa gate.
        # Keep a minimal conservative fallback to avoid hard crash loops.
        output = QAOutput(
            passed=False,
            issues=[],
            target_agent="Draft",
            ticket=None,
            collect_plan=None,
        )
        self._append_qa_log(
            event_type="qa_fallback_output",
            run_state=state,
            payload={"validated_output": output.model_dump(mode="json")},
        )
        return output

    def run_competitor_analysis_review_llm(
        self,
        *,
        analysis_json: dict[str, Any],
        schema_fields: list[str] | None = None,
        industry_hint: str = "",
    ) -> dict[str, Any]:
        competitor = str(analysis_json.get("competitor", "unknown_competitor")).strip() or "unknown_competitor"
        payload = {
            "industry_hint": industry_hint,
            "schema_fields": schema_fields or [],
            "competitor_analysis": analysis_json,
            "constraints": {"query_list_min": 1, "query_list_max": 2},
        }
        shadow_state = RunState(
            industry=industry_hint or "general",
            competitors=[competitor],
            planned_competitors=[competitor],
            user_prompt="analysis_stage_qa",
        )
        self._append_qa_log(
            event_type="qa_analysis_review_input",
            run_state=shadow_state,
            payload={
                "payload_stats": {
                    "competitor": competitor,
                    "field_count": len(analysis_json.get("fields", []) if isinstance(analysis_json.get("fields"), list) else []),
                },
                "user_payload": payload,
            },
        )
        try:
            result = self._invoke_llm_json(
                trace_name="agent.qa.analysis_review",
                system_prompt=QA_ANALYSIS_REVIEW_SYSTEM_PROMPT,
                user_payload=payload,
                metadata={"agent_name": "QACriticAgent", "mode": "analysis_stage", "competitor": competitor, "node_name": "qa"},
                tool_names=["web.search", "web.fetch"],
            )
            self._append_qa_log(
                event_type="qa_analysis_review_output",
                run_state=shadow_state,
                payload={"raw_result": result},
            )
            return result
        except Exception as exc:
            self._append_qa_log(
                event_type="qa_analysis_review_error",
                run_state=shadow_state,
                payload={
                    "trace_name": "agent.qa.analysis_review",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "reason": getattr(exc, "reason", "unknown"),
                    "competitor": competitor,
                },
            )
            raise

    def _invoke_llm_json(
        self,
        *,
        trace_name: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        metadata: dict[str, Any],
        tool_names: list[str],
    ) -> dict[str, Any]:
        if hasattr(self.llm, "invoke_json_with_tools"):
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

    @staticmethod
    def _build_field_evidence_summary(state: RunState) -> list[dict]:
        field_map: dict[tuple[str, str], dict] = defaultdict(
            lambda: {"evidence_count": 0, "source_types": set(), "last_captured_at": "", "sample_urls": []}
        )
        for ev in state.evidences:
            ext = ev.domain_extensions if isinstance(ev.domain_extensions, dict) else {}
            competitor = str(ext.get("competitor", "")).strip()
            field_name = str(ext.get("schema_field", "")).strip()
            if not competitor or not field_name:
                continue
            key = (competitor, field_name)
            row = field_map[key]
            row["evidence_count"] += 1
            row["source_types"].add(ev.source_type)
            captured = ev.captured_at.isoformat() if ev.captured_at else ""
            if captured and (not row["last_captured_at"] or captured > row["last_captured_at"]):
                row["last_captured_at"] = captured
            if len(row["sample_urls"]) < 3 and ev.source_url:
                row["sample_urls"].append(ev.source_url)
        output: list[dict] = []
        for (competitor, field_name), row in sorted(field_map.items()):
            output.append(
                {
                    "competitor": competitor,
                    "field_name": field_name,
                    "evidence_count": row["evidence_count"],
                    "source_types": sorted(row["source_types"]),
                    "last_captured_at": row["last_captured_at"],
                    "sample_urls": row["sample_urls"],
                }
            )
        return output

    @staticmethod
    def _payload_stats(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "top_level_fields": len(payload.keys()),
            "competitor_count": len(payload.get("expected_competitors", [])),
            "schema_field_count": len(payload.get("analysis_schema_plan", [])),
            "evidence_count": len(payload.get("evidences", [])),
            "has_report": payload.get("report") is not None,
        }

    @classmethod
    def _qa_log_dir(cls) -> Path:
        # .../deer-flow/Competitor_Analysis/QA_log
        return Path(__file__).resolve().parents[3] / "QA_log"

    @classmethod
    def _append_qa_log(cls, *, event_type: str, run_state: RunState, payload: dict[str, Any]) -> None:
        try:
            log_dir = cls._qa_log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            target = log_dir / "qa_agent.jsonl"
            record = {
                "event_type": event_type,
                "ts": datetime.now(UTC).isoformat(),
                "run_id": run_state.run_id,
                "attempt": run_state.attempt,
                "industry": run_state.industry,
                "node": "qa",
                "agent": "QACriticAgent",
                "payload": cls._mask_sensitive(payload),
            }
            with target.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("[QACriticAgent] failed to write qa log: %s", exc)

    @classmethod
    def _mask_sensitive(cls, obj: Any, *, parent_key: str = "") -> Any:
        if isinstance(obj, dict):
            masked: dict[str, Any] = {}
            for k, v in obj.items():
                key = str(k)
                if key.lower() in cls._SENSITIVE_KEYS:
                    masked[key] = "***MASKED***"
                    continue
                masked[key] = cls._mask_sensitive(v, parent_key=key)
            return masked
        if isinstance(obj, list):
            return [cls._mask_sensitive(x, parent_key=parent_key) for x in obj]
        if isinstance(obj, str):
            text = obj
            text = re.sub(r"(?i)(bearer)\s+[A-Za-z0-9._\-+/=]+", r"\1 ***MASKED***", text)
            text = re.sub(r"\bsk-[A-Za-z0-9]{8,}\b", "sk-***MASKED***", text)
            text = re.sub(r"\b[A-Za-z0-9_\-]{24,}\b", "***MASKED_LONG_TOKEN***", text)
            if parent_key.lower() in cls._SENSITIVE_KEYS:
                return "***MASKED***"
            return cls._mask_url_query(text)
        return obj

    @classmethod
    def _mask_url_query(cls, text: str) -> str:
        try:
            parsed = urlsplit(text)
            if not parsed.scheme or not parsed.netloc or not parsed.query:
                return text
            query = parse_qsl(parsed.query, keep_blank_values=True)
            masked_query = []
            for k, v in query:
                if k.lower() in cls._URL_SENSITIVE_KEYS:
                    masked_query.append((k, "***MASKED***"))
                else:
                    masked_query.append((k, v))
            new_query = urlencode(masked_query, doseq=True)
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))
        except Exception:  # noqa: BLE001
            return text
