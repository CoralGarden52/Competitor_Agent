from __future__ import annotations


CORE_FIELD_LABELS_ZH: dict[str, str] = {
    'feature_tree': '功能树',
    'strengths': '优势',
    'weaknesses': '劣势',
    'pricing_model': '定价模式',
    'user_feedback': '用户反馈',
}


def field_label_zh(field_name: str) -> str:
    key = str(field_name or '').strip()
    if not key:
        return ''
    if key in CORE_FIELD_LABELS_ZH:
        return CORE_FIELD_LABELS_ZH[key]
    return key.replace('_', ' ')
