from __future__ import annotations


def build_queries(competitor: str, industry: str) -> list[str]:
    return [
        f'{competitor} {industry} 官网 功能',
        f'{competitor} {industry} 价格 套餐',
        f'{competitor} {industry} 用户评价',
        f'{competitor} {industry} 更新日志 发布说明',
        f'{competitor} {industry} 融资 动态 新闻',
    ]
