from __future__ import annotations

import json

import pytest

from app.core.wjx_export import (
    QuestionnaireParseError,
    _extract_wjx_url,
    _parse_cli_response,
    markdown_to_survey_spec,
    markdown_to_wjx_jsonl,
)


def test_markdown_to_survey_spec_parses_common_question_types() -> None:
    markdown = """# 用户调研问卷

## 基础认知
1. 您主要使用哪类会议软件？
A. 腾讯会议
B. Zoom
C. 飞书会议

2. 以下哪些能力会影响您的选择？（多选）
A. 稳定性
B. 价格
C. 会议纪要

## 体验评价
3. 请对整体满意度按 1-5 分评分

4. 您还有什么建议？

5. 请您对当前常用产品的以下能力打分（1分代表非常不满意，5分代表非常满意）
□ 稳定性 1分 2分 3分 4分 5分
□ 易用性 1分 2分 3分 4分 5分
"""

    spec = markdown_to_survey_spec(markdown)

    assert spec.title == '用户调研问卷'
    assert [section.title for section in spec.sections] == ['基础认知', '体验评价']
    assert spec.sections[0].questions[0].question_type == 'single'
    assert spec.sections[0].questions[0].options == ['腾讯会议', 'Zoom', '飞书会议']
    assert spec.sections[0].questions[1].question_type == 'multiple'
    assert spec.sections[1].questions[0].question_type == 'scale'
    assert spec.sections[1].questions[1].question_type == 'text'
    assert spec.sections[1].questions[2].question_type == 'matrix_scale'
    assert spec.sections[1].questions[2].rows == ['稳定性', '易用性']


def test_markdown_to_survey_spec_rejects_no_questions() -> None:
    with pytest.raises(QuestionnaireParseError):
        markdown_to_survey_spec('# 空问卷\n\n## 分组')


def test_markdown_to_wjx_jsonl_outputs_valid_json_lines() -> None:
    _, jsonl = markdown_to_wjx_jsonl(
        """# 问卷

1. 您的角色是？
A. 决策者
B. 使用者
"""
    )

    rows = [json.loads(line) for line in jsonl.splitlines()]
    assert rows[0]['qtype'] == '问卷基础信息'
    assert rows[1]['qtype'] == '单选'
    assert rows[1]['select'] == ['决策者', '使用者']
    assert 'type' not in rows[0]
    assert 'options' not in rows[1]


def test_markdown_to_wjx_jsonl_parses_inline_lettered_options() -> None:
    _, jsonl = markdown_to_wjx_jsonl(
        """# 在线会议工具使用体验调研问卷

1. 您目前是否使用过Zoom或其他同类在线会议产品？ A 正在使用Zoom作为主要会议工具 B 曾经使用过Zoom，现在已经切换为其他产品 C 从未使用过Zoom，正在使用其他同类在线会议产品 D 还没使用过任何付费在线会议产品
"""
    )

    rows = [json.loads(line) for line in jsonl.splitlines()]
    assert rows[1]['qtype'] == '单选'
    assert rows[1]['title'] == '您目前是否使用过Zoom或其他同类在线会议产品？'
    assert rows[1]['select'] == [
        '正在使用Zoom作为主要会议工具',
        '曾经使用过Zoom，现在已经切换为其他产品',
        '从未使用过Zoom，正在使用其他同类在线会议产品',
        '还没使用过任何付费在线会议产品',
    ]


def test_subjective_question_with_which_keyword_exports_as_short_answer() -> None:
    _, jsonl = markdown_to_wjx_jsonl(
        """# 问卷

1. 您对目前市面上的在线会议产品还有哪些其他的优化建议？
_________________________
"""
    )

    rows = [json.loads(line) for line in jsonl.splitlines()]
    assert rows[1] == {
        'qtype': '简答题',
        'title': '您对目前市面上的在线会议产品还有哪些其他的优化建议？',
    }


def test_parse_cli_response_builds_wjx_url_from_domain_and_path() -> None:
    stdout = """{
  "result": true,
  "data": {
    "vid": 366927181,
    "pc_path": "/vm/mo7n3fm.aspx",
    "activity_domain": "https://v.wjx.cn"
  }
}"""

    payload = _parse_cli_response(stdout)

    assert payload['data']['vid'] == 366927181
    assert _extract_wjx_url(payload) == 'https://v.wjx.cn/vm/mo7n3fm.aspx'
