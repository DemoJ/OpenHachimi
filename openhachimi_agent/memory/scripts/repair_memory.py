"""CLI 入口:修复旧版本 bug 留下的长期记忆数据(被反复减半的置信度、旧记忆类型名)。

用法:
    python -m openhachimi_agent.memory.scripts.repair_memory <db_path>
    python -m openhachimi_agent.memory.scripts.repair_memory <db_path> --apply
"""

from __future__ import annotations

import logging
import sys
from argparse import ArgumentParser
from pathlib import Path

from openhachimi_agent.memory.migration import run_legacy_repair


def main() -> int:
    parser = ArgumentParser(description="修复旧版本留下的长期记忆数据")
    parser.add_argument("db_path", type=str, help="long_term_memory.sqlite3 的路径")
    parser.add_argument("--apply", action="store_true", help="实际执行修复(默认 dry-run)")
    args = parser.parse_args()

    db_path = Path(args.db_path).resolve()
    if not db_path.exists():
        print(f"错误:数据库文件不存在 {db_path}")
        return 1

    run_legacy_repair(db_path, dry_run=not args.apply)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
