# -*- coding: utf-8 -*-
# Derived from AlphaSift revision 9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf.
# Licensed under Apache-2.0 and modified for daily_stock_analysis.
"""Explicit, request-local stock universes; never widen a requested scope."""

from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.services.screening.filter import _board_of
from src.services.screening.normalize import normalize_code


class ScreeningTopic(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)
    source: Literal["concept", "industry"] = "concept"


class ScreeningScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    boards: list[Literal["main_board", "chinext", "star_market", "bse"]] = Field(default_factory=list, max_length=4)
    topics: list[ScreeningTopic] = Field(default_factory=list, max_length=5)
    topic_match: Literal["any", "all"] = "any"

    @field_validator("boards")
    @classmethod
    def unique_boards(cls, values):
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def unique_topics(self):
        keys = [(topic.source, topic.name) for topic in self.topics]
        if len(keys) != len(set(keys)):
            raise ValueError("不能重复选择相同题材")
        return self


def apply_scope(df: pd.DataFrame, scope: ScreeningScope, memberships: list[set[str]]) -> tuple[pd.DataFrame, dict]:
    """Intersect a trusted membership set with the snapshot before strategy filters."""
    if len(memberships) != len(scope.topics):
        raise ValueError("题材成分股数据不完整，无法执行筛选")
    if not scope.boards and not scope.topics:
        return df, {"board_count": len(df), "topic_count": len(df)}
    if "code" not in df.columns:
        raise ValueError("行情缺少股票代码，无法核验筛选范围")
    codes = df["code"].map(normalize_code)
    mask = pd.Series(True, index=df.index)
    if scope.boards:
        mask &= codes.map(_board_of).isin(scope.boards)
    board_count = int(mask.sum())
    if scope.topics:
        matches = [codes.isin(members) for members in memberships]
        topic_mask = matches[0].copy()
        for match in matches[1:]:
            topic_mask = topic_mask & match if scope.topic_match == "all" else topic_mask | match
        mask &= topic_mask
    return df.loc[mask].copy(), {"board_count": board_count, "topic_count": int(mask.sum())}
