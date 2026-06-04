from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


class QuestionnaireExportError(Exception):
    status_code = 500

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class QuestionnaireParseError(QuestionnaireExportError):
    status_code = 400


class WjxExportUnavailableError(QuestionnaireExportError):
    status_code = 503


class WjxCliError(QuestionnaireExportError):
    status_code = 502


QuestionType = Literal['single', 'multiple', 'scale', 'matrix_scale', 'text']


@dataclass
class QuestionSpec:
    title: str
    question_type: QuestionType = 'text'
    options: list[str] = field(default_factory=list)
    rows: list[str] = field(default_factory=list)
    required: bool = True


@dataclass
class SectionSpec:
    title: str
    questions: list[QuestionSpec] = field(default_factory=list)


@dataclass
class SurveySpec:
    title: str
    sections: list[SectionSpec] = field(default_factory=list)


QUESTION_RE = re.compile(r'^\s*(\d+)[\.\u3001\)]\s*(.+?)\s*$')
OPTION_RE = re.compile(r'^\s*(?:[-*]\s*)?([A-Za-z\uff21-\uff3a])[\.\u3001\)]\s*(.+?)\s*$')
BULLET_RE = re.compile(r'^\s*[-*]\s+(.+?)\s*$')
MATRIX_SCALE_RE = re.compile(r'^\s*[□☐]\s*(.+?)(?:\s+[1-5]\s*分?){2,}\s*$')
FILL_BLANK_RE = re.compile(r'^[＿_]{3,}$')


def markdown_to_survey_spec(markdown: str) -> SurveySpec:
    title = '未命名问卷'
    sections: list[SectionSpec] = []
    current_section: SectionSpec | None = None
    current_question: QuestionSpec | None = None

    def ensure_section() -> SectionSpec:
        nonlocal current_section
        if current_section is None:
            current_section = SectionSpec(title='默认分组')
            sections.append(current_section)
        return current_section

    def flush_question() -> None:
        nonlocal current_question
        if current_question is None:
            return
        current_question.question_type = infer_question_type_with_rows(
            current_question.title,
            current_question.options,
            current_question.rows,
        )
        ensure_section().questions.append(current_question)
        current_question = None

    for raw_line in str(markdown or '').splitlines():
        line = raw_line.strip()
        if not line or line == '---':
            continue
        if line.startswith('# '):
            title = line[2:].strip() or title
            continue
        if line.startswith('## '):
            flush_question()
            current_section = SectionSpec(title=line[3:].strip() or f'分组{len(sections) + 1}')
            sections.append(current_section)
            continue
        question_match = QUESTION_RE.match(line)
        if question_match:
            flush_question()
            current_question = QuestionSpec(title=question_match.group(2).strip())
            continue
        if current_question is not None:
            if FILL_BLANK_RE.match(line):
                continue
            matrix_scale_match = MATRIX_SCALE_RE.match(line)
            if matrix_scale_match:
                current_question.rows.append(matrix_scale_match.group(1).strip())
                continue
            option_match = OPTION_RE.match(line)
            if option_match:
                current_question.options.append(option_match.group(2).strip())
                continue
            bullet_match = BULLET_RE.match(line)
            if bullet_match:
                option_text = bullet_match.group(1).strip()
                if not re.search(r'^(选项|说明|备注|请按|请从)', option_text):
                    current_question.options.append(option_text)
                continue
            current_question.title = f'{current_question.title} {line}'.strip()

    flush_question()
    sections = [section for section in sections if section.questions]
    if not sections or not any(section.questions for section in sections):
        raise QuestionnaireParseError('问卷 markdown 中没有解析到可导出的题目。')
    return SurveySpec(title=title, sections=sections)


def infer_question_type(title: str, options: list[str]) -> QuestionType:
    return infer_question_type_with_rows(title, options, [])


def infer_question_type_with_rows(title: str, options: list[str], rows: list[str]) -> QuestionType:
    if rows:
        return 'matrix_scale'
    title_text = title
    option_text = ' '.join(options)
    if re.search(r'1\s*[-~到至]\s*5\s*分|5\s*分|量表|满意度|重要性|可能性|同意程度|评分', title_text):
        return 'scale'
    if options and all(re.search(r'^[1-5]\s*分?$', option.strip()) for option in options):
        return 'scale'
    if not options:
        return 'text'
    text = f'{title_text} {option_text}'
    if re.search(r'多选|可多选|选择多项|选择.*项|哪些|以下.*哪些|最多|至少', text):
        return 'multiple'
    return 'single'


def survey_spec_to_jsonl(spec: SurveySpec) -> str:
    rows: list[dict[str, Any]] = [{'qtype': '问卷基础信息', 'title': spec.title, 'atype': 1}]
    for section in spec.sections:
        for question in section.questions:
            rows.append(_question_to_wjx_row(question))
    return '\n'.join(json.dumps(row, ensure_ascii=False, separators=(',', ':')) for row in rows) + '\n'


def markdown_to_wjx_jsonl(markdown: str) -> tuple[SurveySpec, str]:
    spec = markdown_to_survey_spec(markdown)
    return spec, survey_spec_to_jsonl(spec)


def _wjx_question_type(question_type: QuestionType) -> str:
    return {
        'single': '单选',
        'multiple': '多选',
        'scale': '量表题',
        'matrix_scale': '矩阵量表',
        'text': '简答题',
    }[question_type]


def _question_to_wjx_row(question: QuestionSpec) -> dict[str, Any]:
    row: dict[str, Any] = {
        'qtype': _wjx_question_type(question.question_type),
        'title': question.title,
    }
    if question.question_type == 'matrix_scale':
        row['rowtitle'] = question.rows
        row['select'] = ['1', '2', '3', '4', '5']
    elif question.question_type == 'scale':
        row['select'] = _scale_options(question.options)
    elif question.options:
        row['select'] = question.options
    return row


def _scale_options(options: list[str]) -> list[str]:
    cleaned = [option.strip() for option in options if option.strip()]
    if cleaned:
        return cleaned
    return ['1', '2', '3', '4', '5']


def export_questionnaire_with_wjx_cli(
    *,
    run_id: str,
    title: str,
    markdown: str,
    export_dir: Path,
    api_key: str,
    base_url: str,
    cli_path: str,
    publish: bool,
    timeout_sec: int,
) -> dict[str, Any]:
    if not api_key.strip():
        raise WjxExportUnavailableError('未配置 WJX_API_KEY，无法导出到问卷星。')
    if not cli_path.strip():
        raise WjxExportUnavailableError('未配置 wjx-cli 路径。')
    if not _command_available(cli_path):
        raise WjxExportUnavailableError('未找到 wjx-cli，请先运行 npm install -g wjx-cli，或配置 WJX_CLI_PATH。')

    spec, jsonl = markdown_to_wjx_jsonl(markdown)
    export_run_dir = export_dir / run_id
    export_run_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = export_run_dir / 'questionnaire.jsonl'
    jsonl_path.write_text(jsonl, encoding='utf-8')

    command = [cli_path, 'survey', 'create-by-json', '--file', str(jsonl_path)]
    if publish:
        command.append('--publish')
    env = {**os.environ, 'WJX_API_KEY': api_key, 'WJX_BASE_URL': base_url}
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout_sec,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise WjxCliError(f'问卷星 CLI 导出超时（{timeout_sec}s）。') from exc

    stdout = _redact_secret(completed.stdout or '', api_key)
    stderr = _redact_secret(completed.stderr or '', api_key)
    if completed.returncode != 0:
        detail = stderr.strip() or stdout.strip() or f'wjx-cli exited with code {completed.returncode}'
        raise WjxCliError(f'问卷星 CLI 导出失败：{detail}')

    raw_response = _parse_cli_response(stdout)
    vid = _extract_first(raw_response, ('vid', 'id', 'activity_id', 'survey_id'))
    url = _extract_wjx_url(raw_response)
    result = {
        'provider': 'wjx',
        'status': 'success',
        'title': title or spec.title,
        'url': url,
        'vid': str(vid or ''),
        'exported_at': datetime.now(UTC).isoformat(),
        'jsonl_path': str(jsonl_path),
        'raw_response': raw_response,
    }
    return result


def _command_available(command: str) -> bool:
    path = Path(command)
    if path.is_absolute() or path.parent != Path('.'):
        return path.exists()
    return shutil.which(command) is not None


def _parse_cli_response(stdout: str) -> dict[str, Any]:
    parsed_from_text = _parse_json_object_from_text(stdout)
    if parsed_from_text:
        return parsed_from_text
    for line in reversed([item.strip() for item in stdout.splitlines() if item.strip()]):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    url_match = re.search(r'https?://[^\s<>"\'，。；;、]+', stdout)
    return {'stdout': stdout.strip(), 'url': _clean_url(url_match.group(0)) if url_match else ''}


def _extract_first(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in keys:
                value = item.get(key)
                if value:
                    return value
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return ''


def _extract_wjx_url(payload: dict[str, Any]) -> str:
    direct_url = _clean_url(
        _extract_first(payload, ('url', 'link', 'preview_url', 'publish_url', 'activity_url'))
    )
    if direct_url:
        return direct_url

    domain = _clean_url(_extract_first(payload, ('activity_domain', 'domain', 'base_url')))
    path = str(_extract_first(payload, ('pc_path', 'mobile_path', 'path')) or '').strip()
    if domain and path:
        if path.startswith('http://') or path.startswith('https://'):
            return _clean_url(path)
        return f'{domain.rstrip("/")}/{path.lstrip("/")}'

    iframe_url = _clean_url(_extract_first(payload, ('iframe_noauto_url', 'iframe_auto_url')))
    iframe_match = re.search(r'https?://[^\s<>"\']+', iframe_url)
    return _clean_url(iframe_match.group(0)) if iframe_match else ''


def _parse_json_object_from_text(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{', text or ''):
        try:
            parsed, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _clean_url(value: object) -> str:
    url = str(value or '').strip()
    return url.rstrip('",，。；;、)）]】')


def _redact_secret(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, '***')
