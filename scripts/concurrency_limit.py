#!/usr/bin/env python3
"""把请求的并发度解析为不超过硬上限的整数，供 Makefile 防止误设的并行压死主机。

0 表示进程内串行，只对 pytest 的 -n 成立；make 的 --jobs=0 表示不限并发，因此 Makefile
对 CHECK_JOBS 的 0 先行映射为 1（见 Makefile 的 CHECK_JOBS_EFFECTIVE）。
"""

from __future__ import annotations

import os
import re
import sys


def resolve(name: str, raw: str, cap: int) -> int:
    """把 raw 解析为 [0, cap] 内的实际并发数；0 表示进程内串行（仅 pytest -n 的语义）。"""
    if raw == "0":
        return 0
    cpus = os.cpu_count() or 1
    percent = re.fullmatch(r"(\d+)%", raw)
    if raw == "auto":
        wanted = cpus
    elif percent is not None:
        wanted = max(1, cpus * int(percent.group(1)) // 100)
    else:
        try:
            wanted = int(raw)
        except ValueError:
            print(f"{name} 不是可解析的并发值：{raw!r}", file=sys.stderr)
            raise SystemExit(2) from None
    resolved = max(1, min(wanted, cap))
    # auto 是默认值，命中上限属于正常收窄，不提示；显式请求被收窄时提示一次。
    if resolved != wanted and raw != "auto":
        print(f"{name}={raw} 超过上限 {cap}，按 {resolved} 执行", file=sys.stderr)
    return resolved


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: concurrency_limit.py NAME RAW CAP", file=sys.stderr)
        return 2
    try:
        cap = int(argv[3])
    except ValueError:
        print(f"上限不是整数：{argv[3]!r}", file=sys.stderr)
        return 2
    if cap < 1:
        print("上限必须大于等于 1", file=sys.stderr)
        return 2
    print(resolve(argv[1], argv[2], cap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
