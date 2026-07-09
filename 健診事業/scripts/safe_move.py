#!/usr/bin/env python3
"""ファイルの安全な移動・上書き（大原則: 削除は一切しない）。

使い方:
    python3 safe_move.py <元パス> <新パス>

- 移動先に既存ファイルがある場合は、上書きの前に logs/backup/ へ退避コピーする
- すべての操作を logs/復元ログ.csv に「元→新」で追記する（復元ログ）
- rm の代わりに使うことも可能（新パスを logs/backup/ 配下にすればゴミ箱扱いになる）
"""

import csv
import shutil
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # 健診事業/
LOG_DIR = BASE_DIR / "logs"
BACKUP_DIR = LOG_DIR / "backup"
RESTORE_LOG = LOG_DIR / "復元ログ.csv"

LOG_HEADER = ["日時", "操作", "元", "新", "上書き退避先"]


def log_row(operation: str, src: str, dst: str, backup: str = "") -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    is_new = not RESTORE_LOG.exists()
    with RESTORE_LOG.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(LOG_HEADER)
        writer.writerow([datetime.now().isoformat(timespec="seconds"), operation, src, dst, backup])


def safe_move(src: Path, dst: Path) -> None:
    if not src.exists():
        sys.exit(f"エラー: 元ファイルが存在しません: {src}")

    backup_path = ""
    if dst.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUP_DIR / f"{stamp}_{dst.name}"
        shutil.copy2(dst, backup)
        backup_path = str(backup)
        print(f"上書き対象を退避: {dst} -> {backup}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    log_row("move", str(src), str(dst), backup_path)
    print(f"移動: {src} -> {dst}")
    print(f"復元ログに記録済み: {RESTORE_LOG}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    safe_move(Path(sys.argv[1]), Path(sys.argv[2]))
