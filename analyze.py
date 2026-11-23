#!/usr/bin/env python3
"""命令行入口：对 fixtures 下的合成记录运行共现诊断与补扫规划。

用法（从仓库根目录）：
    python3 -I analyze.py --fixtures fixtures --out out --as-of 2026-11-23
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# -I 隔离模式（含 -P）不会自动把脚本目录加入 sys.path，这里显式加入仓库根目录。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from reading_diagnosis import run_analysis, write_reports  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="馆藏数字阅览共现诊断与主动补扫（合成样例）"
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path("fixtures"),
        help="输入目录（primary.csv、support-1.csv、support-2.csv 及可选表）",
    )
    parser.add_argument("--out", type=Path, default=Path("out"), help="报告输出目录")
    parser.add_argument(
        "--as-of",
        type=lambda s: date.fromisoformat(s),
        default=date(2026, 11, 23),
        help="评估基准日（YYYY-MM-DD），默认取题目当前日期 2026-11-23",
    )
    args = parser.parse_args()

    result = run_analysis(args.fixtures, as_of=args.as_of)
    json_path, md_path = write_reports(result, args.out)
    print(f"JSON 诊断结果：{json_path}")
    print(f"Markdown 报告：{md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
