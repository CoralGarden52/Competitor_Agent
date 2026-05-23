from __future__ import annotations


def build_queries(competitor: str, industry: str) -> list[str]:
    return [
        f'{competitor} {industry} official features',
        f'{competitor} {industry} pricing plan',
        f'{competitor} {industry} user reviews',
        f'{competitor} {industry} changelog release notes',
        f'{competitor} {industry} hiring funding news',
    ]
