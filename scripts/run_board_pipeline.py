#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
板块异动日度流程（docs/design/core-backend-design.md 2.9）。

对一个交易日 D 依次执行：补全上一交易日（真实 daily_basic / limit_list_d / 板块目录与行情）
-> 上一交易日概念板块成分（逐板块 dc_member，失败 / 空返回的板块保留现有成分）-> D 的行情入库（daily_basic / limit_list_d 未发布时推算）
-> 板块指标 -> 异动判定，并写入一条 daily_runs 记录（每步结果、阈值快照）。
调度器每个交易日 17:00 自动运行（BOARD_PIPELINE_*），本脚本用于手动运行 / 补跑。
每一步都可重复执行；已有成功的运行时默认跳过，--force 会新增一次运行（旧运行保留为历史）。

使用方法：
    python scripts/run_board_pipeline.py                       # 今天（北京时间）
    python scripts/run_board_pipeline.py --date 2026-11-06
    python scripts/run_board_pipeline.py --date 2026-11-06 --skip-ingest   # 只用本地已入库数据
    python scripts/run_board_pipeline.py --skip-members        # 不折叠上一交易日的概念板块成分
    python scripts/run_board_pipeline.py --force               # 已有成功运行时再跑一次
    python scripts/run_board_pipeline.py --recompute-days 20   # 先按当前口径重算最近 20 个交易日的板块指标
    python scripts/run_board_pipeline.py --industry-membership # 按需运行每周的行业板块成分刷新（最近一个已结束的交易日）
    python scripts/run_board_pipeline.py --industry-membership --date 2026-11-06   # 指定成分日期

环境要求：入库与成分刷新需要 .env 中配置 TUSHARE_TOKEN；--skip-ingest 只读本地表，不发起任何外部请求。
首次使用前先运行 scripts/backfill_market_snapshot.py 回填个股、板块与成分。
--industry-membership 与调度器每周六 10:00（BOARD_INDUSTRY_MEMBERSHIP_*）运行的是同一段逻辑：逐个行业板块取 dc_member，
已折叠过该日的板块不再请求；只做成分刷新，不运行日度流程的其他步骤。
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.services.board_pipeline import run_board_pipeline, run_industry_membership  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="板块异动日度流程：补全上一日 -> 成分 -> 入库 -> 指标 -> 判定")
    parser.add_argument("--date", default=None, help="交易日 YYYY-MM-DD（默认今天，北京时间）")
    parser.add_argument("--force", action="store_true", help="已有成功运行时也再运行一次")
    parser.add_argument("--skip-ingest", action="store_true", help="跳过入库和成分刷新，只用本地数据计算")
    parser.add_argument("--skip-members", action="store_true", help="不折叠上一交易日的概念板块成分")
    parser.add_argument(
        "--industry-membership", action="store_true",
        help="只运行每周的行业板块成分刷新（--date 为成分日期，默认最近一个已结束的交易日），不运行日度流程",
    )
    parser.add_argument("--recompute-days", type=int, default=0, help="先重算最近 N 个交易日的板块指标")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.industry_membership:
        outcome = run_industry_membership(args.date)
        print(json.dumps(outcome, ensure_ascii=False, indent=2, default=str))
        return 1 if outcome["status"] == "failed" else 0
    result = run_board_pipeline(
        args.date,
        force=args.force,
        skip_ingest=args.skip_ingest,
        skip_members=args.skip_members,
        recompute_days=max(args.recompute_days, 0),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
