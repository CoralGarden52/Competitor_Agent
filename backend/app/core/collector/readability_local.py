from __future__ import annotations

import re


def _strip_tags(html: str) -> str:
    text = re.sub(r'(?is)<script.*?>.*?</script>', ' ', html or '')
    text = re.sub(r'(?is)<style.*?>.*?</style>', ' ', text)
    text = re.sub(r'(?is)<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_to_markdown(content: str, *, title: str = 'Untitled', content_type: str = 'markdown') -> str:
    """
    Deerflow-style lightweight extraction:
    - keep markdown if already markdown-like
    - extract readable body if html
    - always return markdown with heading and safe fallback
    """
    body = (content or '').strip()
    if not body:
        return f'# {title}\n\n*No content available*'
    if content_type == 'html' or '<html' in body.lower():
        body = _strip_tags(body)
    if not body:
        body = 'No content could be extracted from this page'
    return f'# {title or "Untitled"}\n\n{body}'
