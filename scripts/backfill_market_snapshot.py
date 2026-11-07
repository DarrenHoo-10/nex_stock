#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
板块 / 全市场快照冷启动回填（docs/design/core-backend-design.md 2.10）。

回填最近 N 个交易日的个股快照（daily / daily_basic / limit_list_d / suspend_d）、
板块目录与 dc_daily 参考行情，再按板块拉取 2024-12-20 以来的 dc_member 成分并压缩进拉链表。

进度保存在数据库本身：已完整入库的日期、成分已刷新到终点的板块都会被跳过，
中断后直接重跑即可续传。请求走 TushareFetcher 的限速，每分钟上限见
MARKET_SNAPSHOT_REQUESTS_PER_MINUTE。

使用方法：
    python scripts/backfill_market_snapshot.py                  # 回填 MARKET_SNAPSHOT_BACKFILL_DAYS 个交易日 + 成分
    python scripts/backfill_market_snapshot.py --days 30
    python scripts/backfill_market_snapshot.py --skip-members   # 只回填个股与板块行情
    python scripts/backfill_market_snapshot.py --members-only   # 每周成分全量刷新
    python scripts/backfill_market_snapshot.py --force          # 重新入库已完整的日期

环境要求：.env 中配置 TUSHARE_TOKEN（daily_basic 2000 积分，limit_list_d 5000 积分，dc_* 6000 积分）。
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import get_config  # noqa: E402
from src.services.market_snapshot_service import MarketSnapshotService, _as_date, _china_today  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="板块 / 全市场快照冷启动回填（可断点续跑）")
    parser.add_argument("--days", type=int, default=None, help="回填的交易日数（默认 MARKET_SNAPSHOT_BACKFILL_DAYS）")
    parser.add_argument("--end-date", default=None, help="终点交易日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--skip-members", action="store_true", help="不回填板块成分")
    parser.add_argument("--members-only", action="store_true", help="只刷新板块成分（每周全量刷新）")
    parser.add_argument("--force", action="store_true", help="重新入库已完整的日期")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.skip_members and args.members_only:
        print("--skip-members 与 --members-only 不能同时使用", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    service = MarketSnapshotService(config=get_config())
    progress = lambda message: print(f"[backfill] {message}", flush=True)  # noqa: E731

    if args.members_only:
        end = _as_date(args.end_date) if args.end_date else _china_today()
        summary = service.refresh_all_membership(end, progress=progress)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 1 if summary["aborted"] else 0

    report = service.backfill(
        end_date=args.end_date,
        days=args.days,
        include_members=not args.skip_members,
        force=args.force,
        progress=progress,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str))
    membership_aborted = bool(report.membership and report.membership.get("aborted"))
    return 1 if (report.aborted or membership_aborted) else 0


if __name__ == "__main__":
    sys.exit(main())
