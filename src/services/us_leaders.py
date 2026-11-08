# -*- coding: utf-8 -*-
"""US leaders: user-editable watch list, symbol mapping and board mapping.

Design: ``docs/design/core-backend-design.md`` 5.3. Pure configuration logic (no I/O except the
read-only board catalog passed in by the caller).

* ``US_LEADERS`` is a JSON array of ``{"symbol", "name", "kind": "stock"|"etf"|"index", "boards": [...],
  "em": "<Eastmoney code>"}`` (``em`` optional); empty / unset = :data:`DEFAULT_US_LEADERS`, ``[]`` = no
  leaders (the feature idles). Invalid entries are skipped with a warning, never fatal.
* ``em`` is the Eastmoney (AKShare) code and the switch for that source: a stock / ETF uses
  ``stock_us_hist`` with ``<market>.<ticker>`` (``105.NVDA`` NASDAQ, ``106.TSM`` NYSE), an index uses the
  ``代码`` of ``index_global_spot_em`` (``SPX``, ``NDX``, ``DJIA``). Without ``em`` a leader is only
  fetched from Tushare / yfinance, exactly as before.
* ``kind`` ``etf`` is a US-listed fund used as a proxy of an index Eastmoney does not publish (SOXX for the
  Philadelphia Semiconductor index, PGJ for the Nasdaq Golden Dragon index): fetched like a stock, but
  judged with the index threshold. PGJ maps to the East Money boards ``互联网服务`` (concept) and
  ``互联网电商`` (industry): ``中概互联`` is not a board name in the catalog.
* ``symbol`` is the storage key in ``stock_daily.code`` and the Tushare spelling (``NVDA`` for
  ``us_daily``; ``SPX`` / ``IXIC`` / ``DJI`` for ``index_global``). :data:`INDEX_SYMBOLS` is the one
  place mapping an index to its Tushare code (``None`` = not on Tushare, yfinance only) and its
  yfinance symbol (``^GSPC`` ...). A stock's yfinance symbol equals its ticker.
* ``boards`` hold A-share board names; :func:`resolve_leader_boards` maps them to ``board_catalog``
  ids (exact name, then normalised name, then contains) and reports what could not be resolved.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

US_LEADERS_CONFIG_KEY = "US_LEADERS"
KIND_STOCK = "stock"
KIND_INDEX = "index"
KIND_ETF = "etf"
DEFAULT_STOCK_MOVE_PCT = 4.0
DEFAULT_INDEX_MOVE_PCT = 1.5
# at most this many boards are taken from the loosest ("contains") matching tier of one board name
MAX_CONTAINS_MATCHES = 5

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
_EM_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-_]{0,19}$")


@dataclass(frozen=True)
class IndexSymbol:
    tushare: Optional[str]  # ``index_global`` ts_code; None = not published by Tushare
    yfinance: str


# canonical storage code -> mapping. Tushare ``index_global`` carries SPX / IXIC / DJI; the rest
# (Nasdaq-100, SOX, Golden Dragon, Russell 2000, VIX) only comes from yfinance (or Eastmoney, see ``em``).
INDEX_SYMBOLS: Dict[str, IndexSymbol] = {
    "SPX": IndexSymbol("SPX", "^GSPC"),
    "IXIC": IndexSymbol("IXIC", "^IXIC"),
    "DJI": IndexSymbol("DJI", "^DJI"),
    "NDX": IndexSymbol(None, "^NDX"),
    "SOX": IndexSymbol(None, "^SOX"),
    "HXC": IndexSymbol(None, "^HXC"),
    "RUT": IndexSymbol(None, "^RUT"),
    "VIX": IndexSymbol(None, "^VIX"),
}
_INDEX_ALIASES = {"GSPC": "SPX", "DJIA": "DJI"}


@dataclass(frozen=True)
class UsLeader:
    symbol: str
    name: str
    kind: str
    boards: Tuple[str, ...] = ()
    em: Optional[str] = None  # Eastmoney code; None = not fetched from Eastmoney

    @property
    def is_index(self) -> bool:
        return self.kind == KIND_INDEX

    @property
    def em_api(self) -> Optional[str]:
        """AKShare interface for this leader, or None when it has no Eastmoney code."""
        if not self.em:
            return None
        return "index_global_spot_em" if self.is_index else "stock_us_hist"

    @property
    def tushare_api(self) -> Optional[str]:
        """Tushare interface for this leader, or None when only yfinance has it."""
        if self.is_index:
            return "index_global" if self.tushare_code else None
        return "us_daily"

    @property
    def tushare_code(self) -> Optional[str]:
        if self.is_index:
            mapped = INDEX_SYMBOLS.get(self.symbol)
            return mapped.tushare if mapped else None
        return self.symbol

    @property
    def yfinance_symbol(self) -> str:
        if self.is_index:
            mapped = INDEX_SYMBOLS.get(self.symbol)
            return mapped.yfinance if mapped else f"^{self.symbol}"
        return self.symbol

    def label(self) -> str:
        return f"{self.name}({self.symbol})" if self.name != self.symbol else self.symbol


def _leader(symbol: str, name: str, kind: str, *boards: str, em: Optional[str] = None) -> UsLeader:
    return UsLeader(symbol=symbol, name=name, kind=kind, boards=tuple(boards), em=em)


# Eastmoney has no Philadelphia Semiconductor / Nasdaq Golden Dragon index quotes and no Nasdaq Composite
# in ``index_global_spot_em``: SOXX / PGJ (NASDAQ-listed ETFs tracking them) stand in, and NDX replaces IXIC.
DEFAULT_US_LEADERS: Tuple[UsLeader, ...] = (
    _leader("NVDA", "英伟达", KIND_STOCK, "算力", "CPO", "半导体", em="105.NVDA"),
    _leader("AVGO", "博通", KIND_STOCK, "CPO", "算力", "半导体", em="105.AVGO"),
    _leader("AMD", "超威半导体", KIND_STOCK, "算力", "半导体", em="105.AMD"),
    _leader("TSM", "台积电", KIND_STOCK, "半导体", "算力", em="106.TSM"),
    _leader("SOXX", "费城半导体ETF（跟踪费城半导体指数）", KIND_ETF, "半导体", "算力", "CPO", em="105.SOXX"),
    _leader("TSLA", "特斯拉", KIND_STOCK, "特斯拉概念", "汽车零部件", em="105.TSLA"),
    _leader("AAPL", "苹果", KIND_STOCK, "消费电子", "苹果概念", em="105.AAPL"),
    _leader("MSFT", "微软", KIND_STOCK, "AI应用", "云计算", em="105.MSFT"),
    _leader("META", "Meta", KIND_STOCK, "AI应用", "云计算", em="105.META"),
    _leader("GOOGL", "谷歌", KIND_STOCK, "AI应用", "云计算", em="105.GOOGL"),
    _leader("AMZN", "亚马逊", KIND_STOCK, "AI应用", "云计算", em="105.AMZN"),
    _leader("PGJ", "中国金龙ETF（跟踪纳斯达克中国金龙指数）", KIND_ETF, "互联网服务", "互联网电商", em="105.PGJ"),
    _leader("SPX", "标普500指数", KIND_INDEX, em="SPX"),
    _leader("NDX", "纳斯达克100指数", KIND_INDEX, em="NDX"),
)


@dataclass
class UsLeadersConfig:
    leaders: List[UsLeader]
    warnings: List[str] = field(default_factory=list)
    source: str = "default"  # default / config


@dataclass(frozen=True)
class UsMoveThresholds:
    stock_pct: float = DEFAULT_STOCK_MOVE_PCT
    index_pct: float = DEFAULT_INDEX_MOVE_PCT

    def for_leader(self, leader: UsLeader) -> float:
        return self.stock_pct if leader.kind == KIND_STOCK else self.index_pct


def thresholds_from_config(config: Any) -> UsMoveThresholds:
    """``US_MOVE_STOCK_PCT`` / ``US_MOVE_INDEX_PCT`` from a Config-like object (defaults when absent)."""
    return UsMoveThresholds(
        stock_pct=_positive(getattr(config, "us_move_stock_pct", None), DEFAULT_STOCK_MOVE_PCT),
        index_pct=_positive(getattr(config, "us_move_index_pct", None), DEFAULT_INDEX_MOVE_PCT),
    )


def _positive(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def normalize_symbol(symbol: Any) -> str:
    """``^gspc`` / ``GSPC`` -> ``SPX``; ``nvda`` -> ``NVDA``; anything invalid -> ``""``."""
    text = str(symbol or "").strip().upper().lstrip("^")
    if not text:
        return ""
    alias = _INDEX_ALIASES.get(text)
    if alias:
        text = alias
    return text if _SYMBOL_PATTERN.match(text) else ""


def load_us_leaders(raw: Optional[str]) -> UsLeadersConfig:
    """Parse the ``US_LEADERS`` JSON value; invalid pieces become warnings, never exceptions."""
    text = (raw or "").strip()
    if not text:
        return UsLeadersConfig(list(DEFAULT_US_LEADERS))
    try:
        payload = json.loads(text)
    except ValueError as exc:
        return UsLeadersConfig(list(DEFAULT_US_LEADERS), [f"{US_LEADERS_CONFIG_KEY} 不是合法 JSON，已使用默认名单: {exc}"])
    if not isinstance(payload, list):
        return UsLeadersConfig(list(DEFAULT_US_LEADERS), [f"{US_LEADERS_CONFIG_KEY} 必须是 JSON 数组，已使用默认名单"])

    leaders: List[UsLeader] = []
    warnings: List[str] = []
    seen = set()
    for index, item in enumerate(payload):
        where = f"{US_LEADERS_CONFIG_KEY}[{index}]"
        if not isinstance(item, dict):
            warnings.append(f"{where} 不是对象，已忽略")
            continue
        symbol = normalize_symbol(item.get("symbol"))
        if not symbol:
            warnings.append(f"{where} 的 symbol 无效 ({item.get('symbol')!r})，已忽略")
            continue
        kind = item.get("kind")
        if kind is None or kind == "":
            kind = KIND_INDEX if symbol in INDEX_SYMBOLS else KIND_STOCK
        if kind not in (KIND_STOCK, KIND_ETF, KIND_INDEX):
            warnings.append(f"{where} ({symbol}) 的 kind 必须是 stock、etf 或 index，已忽略")
            continue
        if symbol in seen:
            warnings.append(f"{where} ({symbol}) 重复，已忽略")
            continue
        boards_raw = item.get("boards", [])
        if boards_raw is None:
            boards_raw = []
        if not isinstance(boards_raw, list) or not all(isinstance(b, str) for b in boards_raw):
            warnings.append(f"{where} ({symbol}) 的 boards 必须是字符串数组，已按空处理")
            boards_raw = []
        boards = tuple(dict.fromkeys(b.strip() for b in boards_raw if b.strip()))
        name = str(item.get("name") or "").strip() or symbol
        em = None
        em_raw = item.get("em")
        if em_raw is not None and em_raw != "":
            em = str(em_raw).strip().upper()
            if not isinstance(em_raw, str) or not _EM_CODE_PATTERN.match(em):
                warnings.append(f"{where} ({symbol}) 的 em 必须是东方财富代码字符串（如 105.NVDA / SPX），已忽略")
                em = None
        seen.add(symbol)
        leaders.append(UsLeader(symbol=symbol, name=name, kind=kind, boards=boards, em=em))
    return UsLeadersConfig(leaders, warnings, source="config")


def get_us_leaders(config: Any = None) -> UsLeadersConfig:
    """Leaders from the live config (``config.us_leaders``); warnings are logged once per call."""
    if config is None:
        from src.config import get_config

        config = get_config()
    loaded = load_us_leaders(getattr(config, "us_leaders", ""))
    for warning in loaded.warnings:
        logger.warning("[USLeaders] %s", warning)
    return loaded


# ----------------------------------------------------------------------
# board mapping
# ----------------------------------------------------------------------
def normalize_board_name(name: Any) -> str:
    """Lower-case, drop whitespace / punctuation and the generic ``概念`` / ``板块`` suffix."""
    text = re.sub(r"[\s\-_·•()（）/]+", "", str(name or "")).lower()
    for suffix in ("概念股", "概念", "板块"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text


@dataclass(frozen=True)
class BoardMatch:
    id: int
    ref: str
    name: str
    board_type: str
    tier: str  # exact / normalized / contains


@dataclass
class BoardResolution:
    by_leader: Dict[str, List[BoardMatch]] = field(default_factory=dict)
    unresolved: Dict[str, List[str]] = field(default_factory=dict)

    def board_ids_for(self, symbols: Sequence[str]) -> Dict[int, List[str]]:
        """``{board_id: [leader symbols]}`` for the given leaders."""
        result: Dict[int, List[str]] = {}
        for symbol in symbols:
            for match in self.by_leader.get(symbol, []):
                result.setdefault(match.id, []).append(symbol)
        return result

    def unresolved_names(self) -> List[str]:
        names: List[str] = []
        for items in self.unresolved.values():
            for item in items:
                if item not in names:
                    names.append(item)
        return names


def match_board_name(name: str, catalog: Sequence[Dict[str, Any]]) -> List[BoardMatch]:
    """Resolve one board name against ``board_catalog`` rows: exact, then normalised, then contains.

    Only the first tier that matches is used. Retired boards are ignored. The contains tier
    (catalog name contains the wanted name, 2+ characters) keeps the shortest names first and at
    most :data:`MAX_CONTAINS_MATCHES` boards, so a generic word cannot fan out to dozens of boards.
    """
    wanted = (name or "").strip()
    if not wanted:
        return []
    active = [b for b in catalog if (b.get("status") or "active") == "active"]

    def build(rows: Sequence[Dict[str, Any]], tier: str) -> List[BoardMatch]:
        return [BoardMatch(b["id"], b.get("ref") or str(b["id"]), b["name"], b.get("board_type", ""), tier) for b in rows]

    exact = [b for b in active if b["name"] == wanted]
    if exact:
        return build(exact, "exact")
    key = normalize_board_name(wanted)
    normalized = [b for b in active if normalize_board_name(b["name"]) == key] if key else []
    if normalized:
        return build(normalized, "normalized")
    if len(key) >= 2:
        contains = [b for b in active if key in normalize_board_name(b["name"])]
        contains.sort(key=lambda b: (len(b["name"]), b["name"]))
        if contains:
            return build(contains[:MAX_CONTAINS_MATCHES], "contains")
    return []


def resolve_leader_boards(leaders: Sequence[UsLeader], catalog: Sequence[Dict[str, Any]]) -> BoardResolution:
    """Map every leader's board names to catalog boards; names without a match are reported."""
    resolution = BoardResolution()
    cache: Dict[str, List[BoardMatch]] = {}
    for leader in leaders:
        matches: List[BoardMatch] = []
        for board_name in leader.boards:
            if board_name not in cache:
                cache[board_name] = match_board_name(board_name, catalog)
            found = cache[board_name]
            if not found:
                resolution.unresolved.setdefault(leader.symbol, []).append(board_name)
            for match in found:
                if all(match.id != m.id for m in matches):
                    matches.append(match)
        resolution.by_leader[leader.symbol] = matches
    return resolution
