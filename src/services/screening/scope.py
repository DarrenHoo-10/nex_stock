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
    source: Literal["concept", "industry", "custom"] = "custom"


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
        keys = [topic.name for topic in self.topics]
        if len(keys) != len(set(keys)):
            raise ValueError("不能重复选择相同题材")
        return self


def apply_scope(df: pd.DataFrame, scope: ScreeningScope) -> tuple[pd.DataFrame, dict]:
    """Apply only deterministic board constraints; themes belong to the model stage."""
    if not scope.boards:
        return df, {"board_count": len(df)}
    if "code" not in df.columns:
        raise ValueError("行情缺少股票代码，无法核验筛选范围")
    codes = df["code"].map(normalize_code)
    mask = codes.map(_board_of).isin(scope.boards)
    return df.loc[mask].copy(), {"board_count": int(mask.sum())}
