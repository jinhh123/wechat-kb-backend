# -*- coding: utf-8 -*-
"""
privacy.py —— 隐私关键词黑名单

命中任意模式就整条丢弃；不打印正文，不调用 AI，不落盘。
"""

from __future__ import annotations

import re

BLACKLIST_KEYWORDS = (
    "身份证",
    "密码",
    "银行卡",
    "家庭住址",
)

# 去掉所有空白后再匹配，防止“身份 证”“密 码”这种拆字
_SPACE_RE = re.compile(r"\s+")

# 允许常见错别字/OCR 变体，以及拼音写法
_BLACKLIST_PATTERNS = (
    re.compile(r"身[份分][证正怔]"),
    re.compile(r"密[码马玛]"),
    re.compile(r"银[行杭][卡咔]"),
    re.compile(r"家[庭廷]住[址趾]"),
    re.compile(r"shenfenzheng"),
    re.compile(r"shenfen"),
    re.compile(r"mima"),
    re.compile(r"yinhangka"),
    re.compile(r"jiatingzhuzhi"),
    re.compile(r"password|passwd"),
)


def normalize(text: str) -> str:
    return _SPACE_RE.sub("", text).casefold()


def contains_blacklisted(text: str | None) -> bool:
    if not text:
        return False
    value = normalize(text)
    return any(pattern.search(value) for pattern in _BLACKLIST_PATTERNS)
