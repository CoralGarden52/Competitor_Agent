from __future__ import annotations


def build_queries(competitor: str, industry: str) -> list[str]:
    return [
        f'{competitor} {industry} 官网 功能',
        f'{competitor} {industry} 价格 套餐',
        f'{competitor} {industry} 用户评价',
    ]
