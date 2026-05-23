from __future__ import annotations

import re
from datetime import UTC, datetime


def mask_pii(text: str) -> str:
    text = re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}', '[masked_email]', text)
    text = re.sub(r'(?<!\\d)(1[3-9]\\d{9})(?!\\d)', '[masked_phone]', text)
    text = re.sub(r'(?<!\\d)(\\d{15}|\\d{17}[0-9Xx])(?!\\d)', '[masked_id]', text)
    return text


def extract_fields(content: str, snippet: str) -> dict:
    merged = f'{snippet} {content}'.lower()
    return {
        'price_detected': any(k in merged for k in ['pricing', 'price', '$', 'free', 'plan']),
        'feature_detected': any(k in merged for k in ['feature', 'integration', 'api', 'automation']),
        'feedback_detected': any(k in merged for k in ['review', 'user', 'complaint', 'support']),
        'captured_at': datetime.now(UTC).isoformat(),
    }
