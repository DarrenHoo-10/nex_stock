# -*- coding: utf-8 -*-
"""Mask amounts, share counts and costs before text leaves for a recall service.

Holdings facts live in the ledger, so recalled conversations never need the
numbers; masking also keeps them off the remote server. Stock codes,
percentages and dates are intentionally left alone.
"""

from __future__ import annotations

import re

_NUM = r"\d[\d,，]*(?:\.\d+)?"
_MAGNITUDE = r"(?:\s*[万亿])?"

# 成本 12.5 / 成本价是 12.5元 / 买入均价：¥12.5  ->  成本 [价格]
_COST_RE = re.compile(
    r"(持仓成本|买入成本|买入均价|买入价|成本均价|成本价|建仓价|成本|均价)"
    r"\s*(?:是|为|在|约|大约|大概|:|：)?\s*(?:[¥￥$]\s*)?" + _NUM + _MAGNITUDE
    + r"(?:\s*(?:元|块钱|块|美元|港元|美金))?"
)
# 1000股 / 3万股 / 5手  ->  [数量]股
_QUANTITY_RE = re.compile(_NUM + _MAGNITUDE + r"\s*(股|手|张|份)")
# ¥1,000 / $20 / 5万元 / 200元  ->  [金额]
_CURRENCY_PREFIX_RE = re.compile(r"[¥￥$]\s*" + _NUM + _MAGNITUDE)
_CURRENCY_SUFFIX_RE = re.compile(_NUM + r"\s*(?:万元|亿元|元|块钱|块|美元|港元|美金|万|亿)")


def mask_sensitive_numbers(text: str) -> str:
    """Return ``text`` with cost, share-count and amount figures masked."""
    if not text:
        return text
    masked = _COST_RE.sub(lambda m: f"{m.group(1)} [价格]", text)
    masked = _QUANTITY_RE.sub(lambda m: f"[数量]{m.group(1)}", masked)
    masked = _CURRENCY_PREFIX_RE.sub("[金额]", masked)
    masked = _CURRENCY_SUFFIX_RE.sub("[金额]", masked)
    return masked
