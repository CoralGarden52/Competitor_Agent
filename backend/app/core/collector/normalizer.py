from __future__ import annotations

import hashlib
from datetime import UTC, datetime


def normalize_url(url: str) -> str:
    return url.strip().rstrip('/')


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8', errors='ignore')).hexdigest()


def recency_score(captured_at: datetime) -> float:
    days = (datetime.now(UTC) - captured_at).days
    if days <= 30:
        return 1.0
    if days <= 180:
        return 0.7
    if days <= 365:
        return 0.4
    return 0.2
