# -*- coding: utf-8 -*-
"""privacy.py —— 隐私关键词黑名单，命中即整条丢弃。"""

from __future__ import annotations

BLACKLIST_KEYWORDS = (
    "身份证",
    "密码",
    "银行卡",
    "家庭住址",
)


def contains_blacklisted(text: str | None) -> bool:
    if not text:
        return False
    value = text.casefold()
    return any(keyword.casefold() in value for keyword in BLACKLIST_KEYWORDS)
