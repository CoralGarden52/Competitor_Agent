from __future__ import annotations

import asyncio


def fetch_with_deerflow(url: str) -> str:
    try:
        from deerflow.community.jina_ai.tools import web_fetch_tool

        if hasattr(web_fetch_tool, 'coroutine') and web_fetch_tool.coroutine is not None:
            return str(asyncio.run(web_fetch_tool.coroutine(url=url)))
        return str(web_fetch_tool.invoke({'url': url}))
    except Exception:
        return ''
