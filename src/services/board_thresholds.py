# -*- coding: utf-8 -*-
"""Board anomaly thresholds: definitions, defaults, overrides and validation.

Single source for the numbers in docs/design/core-product-spec.md section 3.2 / 3.3.
Overrides are stored as a JSON object in the config key ``BOARD_ANOMALY_THRESHOLDS``
(written by ``PUT /api/v1/boards/thresholds``); an empty value means "all defaults".

Dict-valued thresholds accept partial overrides (``{"excess_floor": {"concept": 3}}``
keeps the industry default). A value is reported as overridden only when it differs
from the default.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

THRESHOLDS_CONFIG_KEY = "BOARD_ANOMALY_THRESHOLDS"

Number = float


@dataclass(frozen=True)
class FieldSpec:
    """One numeric value (a scalar threshold, or one member of a dict threshold)."""

    label: str
    default: Number
    minimum: Optional[Number] = None
    maximum: Optional[Number] = None
    min_exclusive: bool = False
    integer: bool = False


@dataclass(frozen=True)
class ThresholdDef:
    key: str
    label: str
    evidence: str  # medium / low / inferred (docs/design/core-product-spec.md 3.2)
    unit: Optional[str]
    fields: Mapping[str, FieldSpec]  # scalar thresholds use the single field name ""
    display: Callable[[Any], str]
    note: Optional[str] = None
    group: str = "rules"

    @property
    def is_dict(self) -> bool:
        return tuple(self.fields) != ("",)

    @property
    def default(self) -> Any:
        if not self.is_dict:
            return _num(self.fields[""].default, self.fields[""].integer)
        return {name: _num(spec.default, spec.integer) for name, spec in self.fields.items()}


def _num(value: Number, integer: bool) -> Any:
    return int(round(value)) if integer else float(value)


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _scalar(label: str, default: Number, **kwargs: Any) -> Mapping[str, FieldSpec]:
    return {"": FieldSpec(label, default, **kwargs)}


THRESHOLD_DEFS: Tuple[ThresholdDef, ...] = (
    ThresholdDef(
        "amount_ratio_mild", "温和放量", "medium", "倍",
        _scalar("倍数", 1.5, minimum=1.0, maximum=20.0, min_exclusive=True),
        lambda v: f"成交额 ≥ 近 5 日均值 {_fmt(v)} 倍",
        "个股「量比 1.5–2.5 温和放量」口径移植到板块。",
    ),
    ThresholdDef(
        "amount_ratio_strong", "明显放量", "medium", "倍",
        _scalar("倍数", 2.5, minimum=1.0, maximum=50.0, min_exclusive=True),
        lambda v: f"成交额 ≥ 近 5 日均值 {_fmt(v)} 倍",
        "板块为汇总值，波动小于个股，可能偏严。",
    ),
    ThresholdDef(
        "rank_strong_top_pct", "强势排名", "medium", "%",
        _scalar("前 N%", 10.0, minimum=0.0, maximum=50.0, min_exclusive=True),
        lambda v: f"当日超额收益排在同类板块前 {_fmt(v)}%",
        "对应相对强度 RPS ≥ 90。",
    ),
    ThresholdDef(
        "rank_weak_bottom_pct", "弱势排名", "inferred", "%",
        _scalar("后 N%", 10.0, minimum=0.0, maximum=50.0, min_exclusive=True),
        lambda v: f"当日超额收益排在同类板块后 {_fmt(v)}%",
        "与强势排名对称，无直接来源。",
    ),
    ThresholdDef(
        "excess_floor", "绝对超额下限", "inferred", "%",
        {
            "industry": FieldSpec("行业", 1.5, minimum=0.0, maximum=20.0, min_exclusive=True),
            "concept": FieldSpec("概念", 2.5, minimum=0.0, maximum=20.0, min_exclusive=True),
        },
        lambda v: f"行业 +{_fmt(v['industry'])}%，概念 +{_fmt(v['concept'])}%",
        "避免平淡市场里也报出异动；必须用历史数据回测校准。",
    ),
    ThresholdDef(
        "breadth_pct", "上涨家数占比", "low", "%",
        {
            "strong": FieldSpec("强", 70.0, minimum=50.0, maximum=100.0, min_exclusive=True),
            "weak": FieldSpec("弱", 30.0, minimum=0.0, maximum=50.0, min_exclusive=True),
        },
        lambda v: f"强 ≥ {_fmt(v['strong'])}%，弱 ≤ {_fmt(v['weak'])}%",
        "全市场宽度口径移植到板块。",
    ),
    ThresholdDef(
        "limit_up_effect", "板块效应（概念）", "low", "家",
        _scalar("涨停家数", 3, minimum=1, maximum=200, integer=True),
        lambda v: f"板块内涨停 ≥ {_fmt(v)} 家",
        "行业板块不使用涨停类条件。",
    ),
    ThresholdDef(
        "mainline", "主线（概念）", "low", None,
        {
            "limit_up": FieldSpec("涨停家数", 5, minimum=1, maximum=200, integer=True),
            "days": FieldSpec("连续天数", 2, minimum=1, maximum=10, integer=True),
            "max_consecutive": FieldSpec("最高连板", 3, minimum=1, maximum=20, integer=True),
            "min_days": FieldSpec("持续天数", 5, minimum=1, maximum=30, integer=True),
        },
        lambda v: (
            f"涨停连续 {_fmt(v['days'])} 日 ≥ {_fmt(v['limit_up'])} 家，"
            f"最高连板 ≥ {_fmt(v['max_consecutive'])}，持续 ≥ {_fmt(v['min_days'])} 天"
        ),
        "「持续」按连续跑赢基准的天数计。",
    ),
    ThresholdDef(
        "break_rate_warn", "炸板率预警", "medium", "%",
        _scalar("炸板率", 40.0, minimum=0.0, maximum=100.0, min_exclusive=True),
        lambda v: f"板块内炸板率 ≥ {_fmt(v)}%",
        "全市场长期均值约 30%，下行期约 40%。",
    ),
    ThresholdDef(
        "break_min_touched", "炸板率样本下限", "inferred", "家",
        _scalar("触及涨停家数", 5, minimum=1, maximum=100, integer=True),
        lambda v: f"触及涨停不足 {_fmt(v)} 家时不计算炸板率",
        "写入板块日度指标；修改后需重算历史指标才会生效。",
        group="metrics",
    ),
    ThresholdDef(
        "confirm", "持续确认", "low", "日",
        {
            "days": FieldSpec("连续跑赢天数", 3, minimum=2, maximum=10, integer=True),
            "strong_days": FieldSpec("其中强日天数", 2, minimum=1, maximum=10, integer=True),
        },
        lambda v: f"连续 {_fmt(v['days'])} 日跑赢基准，其中至少 {_fmt(v['strong_days'])} 天为强日",
        "持续走强只描述过去的状态，不作为买入信号。",
    ),
    ThresholdDef(
        "strengthening_rank_5d_top_pct", "持续走强的 5 日排名", "low", "%",
        _scalar("前 N%", 10.0, minimum=0.0, maximum=50.0, min_exclusive=True),
        lambda v: f"近 5 日超额排在同类板块前 {_fmt(v)}%",
    ),
    ThresholdDef(
        "first_launch", "首次启动", "inferred", None,
        {
            "prior_days": FieldSpec("此前无强日的天数", 5, minimum=1, maximum=20, integer=True),
            "not_top_pct": FieldSpec("此前 5 日超额不在前 N%", 30.0, minimum=0.0, maximum=100.0, min_exclusive=True),
            "breadth_pct": FieldSpec("上涨占比", 60.0, minimum=0.0, maximum=100.0, min_exclusive=True),
        },
        lambda v: (
            f"前 {_fmt(v['prior_days'])} 日无强日，此前 5 日超额不在前 {_fmt(v['not_top_pct'])}%，"
            f"上涨占比 ≥ {_fmt(v['breadth_pct'])}%"
        ),
        "「近 5 日超额」取前一交易日的 5 日排名，避免当日强势抬高自己的排名。",
    ),
    ThresholdDef(
        "divergence", "高位分歧", "inferred", None,
        {
            "min_conditions": FieldSpec("满足条数", 2, minimum=1, maximum=4, integer=True),
            "breadth_pct": FieldSpec("上涨占比不高于", 50.0, minimum=0.0, maximum=100.0, min_exclusive=True),
            "limit_up_drop_pct": FieldSpec("涨停数较前日减少", 50.0, minimum=0.0, maximum=100.0, min_exclusive=True),
        },
        lambda v: (
            f"满足 {_fmt(v['min_conditions'])} 条：超额 ≤ 0 或上涨占比 ≤ {_fmt(v['breadth_pct'])}%、放量滞涨、炸板率预警、"
            f"涨停数较前日减少 ≥ {_fmt(v['limit_up_drop_pct'])}%"
        ),
    ),
    ThresholdDef(
        "ebb_peak_pct", "退潮成交", "low", "%",
        _scalar("峰值占比", 50.0, minimum=0.0, maximum=100.0, min_exclusive=True),
        lambda v: f"成交额低于近期峰值的 {_fmt(v)}%",
    ),
    ThresholdDef(
        "ebb", "退潮", "inferred", None,
        {
            "lookback_days": FieldSpec("回看天数", 10, minimum=2, maximum=40, integer=True),
            "consecutive_days": FieldSpec("连续天数", 2, minimum=1, maximum=5, integer=True),
            "min_conditions": FieldSpec("满足条数", 2, minimum=1, maximum=5, integer=True),
            "limit_up_max": FieldSpec("涨停不超过", 1, minimum=0, maximum=50, integer=True),
            "rank_5d_out_pct": FieldSpec("5 日排名跌出前 N%", 50.0, minimum=0.0, maximum=100.0, min_exclusive=True),
        },
        lambda v: (
            f"近 {_fmt(v['lookback_days'])} 日出现过持续走强或高位分歧；连续 {_fmt(v['consecutive_days'])} 日满足 "
            f"{_fmt(v['min_conditions'])} 条（超额 < 0、涨停 ≤ {_fmt(v['limit_up_max'])} 家、成交额低于峰值、"
            f"上涨占比走弱、5 日排名跌出前 {_fmt(v['rank_5d_out_pct'])}%）"
        ),
    ),
    ThresholdDef(
        "abnormal_drop", "异常下跌（概念补充）", "inferred", None,
        {
            "limit_down_count": FieldSpec("跌停家数", 3, minimum=1, maximum=200, integer=True),
            "drop7_ratio_pct": FieldSpec("跌幅超 7% 的个股占比", 30.0, minimum=0.0, maximum=100.0, min_exclusive=True),
        },
        lambda v: f"概念另需跌停 ≥ {_fmt(v['limit_down_count'])} 家，或跌幅超 7% 的个股占比 ≥ {_fmt(v['drop7_ratio_pct'])}%",
    ),
    ThresholdDef(
        "broad_rally_pct", "普涨日降权", "low", "%",
        _scalar("全 A 涨幅", 2.0, minimum=0.0, maximum=20.0, min_exclusive=True),
        lambda v: f"全 A 等权涨幅 ≥ {_fmt(v)}% 时，涨停与上涨家数类条件不参与判定，只看排名",
    ),
    ThresholdDef(
        "min_members", "最少成分股", "inferred", "只",
        _scalar("有效成分", 10, minimum=1, maximum=500, integer=True),
        lambda v: f"有效成分股不足 {_fmt(v)} 只的板块不参与判定",
        "写入板块日度指标；修改后需重算历史指标才会生效。",
        group="metrics",
    ),
    ThresholdDef(
        "broad_max_members", "宽基概念上限", "inferred", "只",
        _scalar("成分数", 800, minimum=50, maximum=6000, integer=True),
        lambda v: f"成分股超过 {_fmt(v)} 只视为宽基，不参与判定",
        "写入板块日度指标；修改后需重算历史指标才会生效。",
        group="metrics",
    ),
    ThresholdDef(
        "single_stock_share_pct", "单股驱动", "inferred", "%",
        _scalar("贡献占比", 50.0, minimum=0.0, maximum=100.0, min_exclusive=True),
        lambda v: f"单只股票贡献超过板块涨幅 {_fmt(v)}% 时标注「单股驱动」，不报异动",
    ),
    ThresholdDef(
        "cooling_days", "新概念冷却", "inferred", "个交易日",
        _scalar("交易日", 10, minimum=0, maximum=60, integer=True),
        lambda v: f"新概念上线 {_fmt(v)} 个交易日内只记录，不判「首次启动」",
        "写入板块日度指标；修改后需重算历史指标才会生效。",
        group="metrics",
    ),
    ThresholdDef(
        "merge_jaccard", "重复概念合并", "inferred", None,
        _scalar("重合度", 0.6, minimum=0.0, maximum=1.0, min_exclusive=True),
        lambda v: f"成分股重合度（Jaccard）≥ {_fmt(v)} 的概念合并展示",
    ),
)

_DEF_BY_KEY: Dict[str, ThresholdDef] = {d.key: d for d in THRESHOLD_DEFS}
DEFAULT_THRESHOLDS: Dict[str, Any] = {d.key: d.default for d in THRESHOLD_DEFS}


def threshold_def(key: str) -> Optional[ThresholdDef]:
    return _DEF_BY_KEY.get(key)


# ---------------------------------------------------------------------------
# parsing / validation
# ---------------------------------------------------------------------------


def parse_overrides(raw: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Parse the stored config text into an override mapping.

    Never raises: unreadable content yields ``({}, [reason])`` so a bad stored value
    degrades to the defaults instead of breaking the run.
    """
    if raw is None:
        return {}, []
    if isinstance(raw, Mapping):
        return dict(raw), []
    text = str(raw).strip()
    if not text:
        return {}, []
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        return {}, [f"BOARD_ANOMALY_THRESHOLDS 不是合法 JSON，已使用默认阈值: {exc}"]
    if not isinstance(value, dict):
        return {}, ["BOARD_ANOMALY_THRESHOLDS 必须是 JSON 对象，已使用默认阈值"]
    return value, []


def _check_number(path: str, value: Any, spec: FieldSpec) -> Tuple[Optional[Number], Optional[str]]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"{path} 必须为数值"
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None, f"{path} 必须为有限数值"
    if spec.integer and not number.is_integer():
        return None, f"{path} 必须为整数"
    if spec.minimum is not None:
        too_low = number <= spec.minimum if spec.min_exclusive else number < spec.minimum
        if too_low:
            relation = "大于" if spec.min_exclusive else "不小于"
            return None, f"{path} 必须{relation} {_fmt(spec.minimum)}"
    if spec.maximum is not None and number > spec.maximum:
        return None, f"{path} 不能大于 {_fmt(spec.maximum)}"
    return number, None


def validate_overrides(overrides: Mapping[str, Any]) -> List[str]:
    """Return human-readable problems (empty = valid)."""
    errors: List[str] = []
    if not isinstance(overrides, Mapping):
        return ["overrides 必须是对象"]
    for key, value in overrides.items():
        definition = _DEF_BY_KEY.get(key)
        if definition is None:
            errors.append(f"未知阈值 {key}")
            continue
        if not definition.is_dict:
            _, error = _check_number(key, value, definition.fields[""])
            if error:
                errors.append(error)
            continue
        if not isinstance(value, Mapping):
            errors.append(f"{key} 必须是对象，字段: {', '.join(definition.fields)}")
            continue
        for name, item in value.items():
            spec = definition.fields.get(name)
            if spec is None:
                errors.append(f"{key} 没有字段 {name}，可用字段: {', '.join(definition.fields)}")
                continue
            _, error = _check_number(f"{key}.{name}", item, spec)
            if error:
                errors.append(error)
    if errors:
        return errors
    effective = effective_thresholds(overrides)
    if effective["amount_ratio_strong"] < effective["amount_ratio_mild"]:
        errors.append("amount_ratio_strong 不能小于 amount_ratio_mild")
    if effective["breadth_pct"]["weak"] >= effective["breadth_pct"]["strong"]:
        errors.append("breadth_pct.weak 必须小于 breadth_pct.strong")
    if effective["confirm"]["strong_days"] > effective["confirm"]["days"]:
        errors.append("confirm.strong_days 不能大于 confirm.days")
    return errors


def effective_thresholds(overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Defaults merged with valid overrides; invalid or unknown entries are ignored."""
    result = copy.deepcopy(DEFAULT_THRESHOLDS)
    for key, value in (overrides or {}).items():
        definition = _DEF_BY_KEY.get(key)
        if definition is None:
            continue
        if not definition.is_dict:
            number, error = _check_number(key, value, definition.fields[""])
            if error is None:
                result[key] = _num(number, definition.fields[""].integer)  # type: ignore[arg-type]
            continue
        if not isinstance(value, Mapping):
            continue
        for name, item in value.items():
            spec = definition.fields.get(name)
            if spec is None:
                continue
            number, error = _check_number(f"{key}.{name}", item, spec)
            if error is None:
                result[key][name] = _num(number, spec.integer)  # type: ignore[arg-type]
    return result


def overridden_keys(effective: Mapping[str, Any]) -> Set[str]:
    return {key for key, value in effective.items() if value != DEFAULT_THRESHOLDS.get(key)}


def resolve_thresholds(raw: Any) -> Tuple[Dict[str, Any], List[str]]:
    """Parse + merge the stored config text. Returns ``(effective, warnings)``."""
    overrides, warnings = parse_overrides(raw)
    problems = validate_overrides(overrides)
    if problems:
        # An invalid or incoherent override set is not applied piecemeal: all defaults.
        warnings.extend(f"阈值覆盖被忽略: {problem}" for problem in problems)
        return effective_thresholds({}), warnings
    return effective_thresholds(overrides), warnings


def load_current_thresholds() -> Tuple[Dict[str, Any], List[str]]:
    """Effective thresholds from the live config (``BOARD_ANOMALY_THRESHOLDS``)."""
    from src.config import get_config

    return resolve_thresholds(getattr(get_config(), "board_anomaly_thresholds", ""))


def describe_thresholds(effective: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Items shaped like ``ThresholdItem`` (api/v1/schemas/boards.py)."""
    overridden = overridden_keys(effective)
    items: List[Dict[str, Any]] = []
    for definition in THRESHOLD_DEFS:
        value = effective[definition.key]
        items.append(
            {
                "key": definition.key,
                "label": definition.label,
                "value": copy.deepcopy(value),
                "default": copy.deepcopy(definition.default),
                "display": definition.display(value),
                "unit": definition.unit,
                "evidence": definition.evidence,
                "note": definition.note,
                "overridden": definition.key in overridden,
            }
        )
    return items


def threshold_keys() -> Sequence[str]:
    return tuple(_DEF_BY_KEY)
