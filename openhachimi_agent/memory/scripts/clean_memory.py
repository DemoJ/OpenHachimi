"""CLI 入口:清理 v1 长期记忆污染。

用法:
    python -m openhachimi_agent.memory.scripts.clean_memory <db_path>
    python -m openhachimi_agent.memory.scripts.clean_memory <db_path> --apply
    python -m openhachimi_agent.memory.scripts.clean_memory <db_path> --apply --limit 10000
"""

from __future__ import annotations

import logging
import sys
from argparse import ArgumentParser
from pathlib import Path

from openhachimi_agent.memory.migration import run_contamination_cleanup


def main() -> int:
    parser = ArgumentParser(description="清理 v1 长期记忆污染")
    parser.add_argument("db_path", type=str, help="long_term_memory.sqlite3 的路径")
    parser.add_argument("--apply", action="store_true", help="实际执行清理(默认 dry-run)")
    parser.add_argument("--limit", type=int, default=5000, help="最多检查多少条活跃 atom (默认 5000)")
    args = parser.parse_args()

    db_path = Path(args.db_path).resolve()
    if not db_path.exists():
        print(f"错误:数据库文件不存在 {db_path}")
        return 1

    run_contamination_cleanup(db_path, dry_run=not args.apply, limit=args.limit)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())