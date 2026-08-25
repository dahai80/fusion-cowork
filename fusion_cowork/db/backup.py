"""数据库备份/恢复 (v0.4.0 Stage 3).

pg_dump wrapper (subprocess): backup() 输出 .sql.gz; restore() 警告 + 确认。
仅 postgres backend (SQLite 用文件拷贝即可, 非 cloud 路径)。

CLI: fusion-cowork db backup|restore
"""

from __future__ import annotations

import gzip
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class BackupManager:
    """pg_dump / pg_restore wrapper。

    dsn: Postgres 连接串 (postgresql://user:pass@host:port/db)
    pg_dump_path / pg_restore_path: 可执行路径 (缺则 shutil.which 探测)
    """

    def __init__(
        self,
        dsn: str,
        pg_dump_path: Optional[str] = None,
        pg_restore_path: Optional[str] = None,
    ):
        self.dsn = dsn
        self.pg_dump_path = pg_dump_path or shutil.which("pg_dump")
        self.pg_restore_path = pg_restore_path or shutil.which("psql")

    def backup(self, dest: str | Path) -> Path:
        """pg_dump 备份到 dest (.sql.gz, gzip 压缩)。返回输出路径。

        失败 fail-visible: pg_dump 缺或非零退出 → raise, 不静默返空文件。
        """
        if not self.pg_dump_path:
            raise RuntimeError("pg_dump 未找到 — 安装 postgresql-client 或显式传 pg_dump_path")
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        sql_path = dest_path.with_suffix(".sql")
        logger.info(f"pg_dump 备份 → {sql_path} (dsn={_safe_dsn(self.dsn)})")
        proc = subprocess.run(
            [self.pg_dump_path, "--no-owner", "--no-privileges", self.dsn],
            stdout=open(sql_path, "w"),
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            logger.error(f"pg_dump 失败 (exit={proc.returncode}): {proc.stderr[:500]}")
            raise RuntimeError(f"pg_dump 失败: {proc.stderr[:200]}")
        with (
            open(sql_path, "rb") as fin,
            gzip.open(str(dest_path) if str(dest_path).endswith(".gz") else str(dest_path) + ".gz", "wb") as fout,
        ):
            shutil.copyfileobj(fin, fout)
        sql_path.unlink()
        gz_path = dest_path if str(dest_path).endswith(".gz") else dest_path.parent / (dest_path.name + ".gz")
        logger.info(f"备份完成: {gz_path} ({gz_path.stat().st_size} bytes)")
        return gz_path

    def restore(self, src: str | Path, confirm: bool = False) -> None:
        """从 .sql.gz 恢复。破坏性操作 — confirm=False 拒绝执行。

        用 psql 执行解压后的 SQL。还原前必须确认 (覆盖现有数据)。
        """
        if not confirm:
            raise RuntimeError("restore 是破坏性操作 — 传 confirm=True 确认覆盖现有数据库")
        if not self.pg_restore_path:
            raise RuntimeError("psql 未找到 — 安装 postgresql-client 或显式传 pg_restore_path")
        src_path = Path(src)
        if not src_path.exists():
            raise FileNotFoundError(f"备份文件不存在: {src_path}")
        logger.warning(f"开始恢复 {src_path} → {_safe_dsn(self.dsn)} (覆盖现有数据!)")
        with gzip.open(str(src_path), "rb") as fin:
            proc = subprocess.run(
                [self.pg_restore_path, "-v", "--no-owner", self.dsn],
                stdin=fin,
                capture_output=True,
            )
        if proc.returncode != 0:
            logger.error(f"psql 恢复失败 (exit={proc.returncode}): {proc.stderr.decode('utf-8', 'ignore')[:500]}")
            raise RuntimeError("psql 恢复失败 — 见日志")
        logger.info(f"恢复完成: {src_path}")


def _safe_dsn(dsn: str) -> str:
    """dsn 日志脱敏 — 隐藏密码。"""
    import re

    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn)
