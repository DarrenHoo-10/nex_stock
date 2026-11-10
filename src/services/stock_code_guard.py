# -*- coding: utf-8 -*-
"""Input guards for A-share codes that would otherwise silently fetch another security.

* 北交所 moved to ``920xxx`` codes; the old ``43/83/87xxxx`` codes still return "zombie" data in some
  sources (Tencent: frozen price, zero turnover). ``resolve_bse_code`` maps an old code to its new one
  through 巨潮's official orgId map, where a new code's orgId embeds the old code
  (``920002 -> gfbj0873843``); an old code without a mapping is rejected.
* ``require_stock_code`` rejects index codes on stock-only endpoints (events, Q&A, research):
  ``sh000001`` is the SSE index, but a bare ``000001`` read as a stock is 平安银行.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)

LEGACY_BSE_PREFIXES = ("43", "83", "87")
_ORGID_TIMEOUT_SEC = 10.0


class LegacyBseCodeError(ValueError):
    """An old 北交所 code with no known new code."""


def is_legacy_bse_code(code: str) -> bool:
    text = str(code or "").strip()
    return bool(re.fullmatch(r"\d{6}", text)) and text.startswith(LEGACY_BSE_PREFIXES)


def _legacy_map(mapping: Dict[str, str]) -> Dict[str, str]:
    """old code -> new 920 code, read from the orgIds of the new codes."""
    legacy: Dict[str, str] = {}
    for code, org in mapping.items():
        if code.startswith("92") and str(org).startswith("gfbj0"):
            old = str(org)[len("gfbj0"):]
            if is_legacy_bse_code(old):
                legacy[old] = code
    return legacy


def new_bse_code(code: str) -> Optional[str]:
    """New ``920`` code of an old 北交所 code (``None`` when 巨潮's map does not know it)."""
    from src.services import news_cninfo_source

    mapping = news_cninfo_source.orgid_map(timeout_sec=_ORGID_TIMEOUT_SEC)
    return _legacy_map(mapping).get(str(code).strip())


def resolve_bse_code(code: str) -> str:
    """``code`` itself, or the new code when it is an old 北交所 code (raises when unknown)."""
    text = str(code or "").strip()
    if not is_legacy_bse_code(text):
        return text
    new = new_bse_code(text)
    if new is None:
        raise LegacyBseCodeError(f"{text} 是北交所旧代码，已停用，请改用 920 开头的新代码")
    logger.info("[代码] 北交所旧代码 %s 换成新代码 %s", text, new)
    return new


def require_stock_code(code: str) -> str:
    """A 6-digit A-share stock code; raises ``ValueError`` for indices and anything else."""
    from src.services.stock_list_parser import ParseStatus, parse_analysis_target

    text = str(code or "").strip()
    target = parse_analysis_target(text)
    if target.asset_type == ParseStatus.INDEX:
        raise ValueError(f"{text} 是指数代码，这里只接受个股代码")
    digits = re.sub(r"(?i)^(sh|sz|bj)|\.(sh|sz|bj)$", "", text)
    if not re.fullmatch(r"\d{6}", digits):
        raise ValueError(f"{text} 不是 6 位 A 股代码")
    return resolve_bse_code(digits)
