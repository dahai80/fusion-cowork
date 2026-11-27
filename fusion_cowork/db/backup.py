"""Database backup/restore (v0.4.0 Stage 3).

pg_dump wrapper (subprocess): backup() writes a .sql.gz; restore() warns + requires confirmation.
Postgres backend only (SQLite just copies the file, not a cloud path).

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
    """pg_dump / pg_restore wrapper.

    dsn: Postgres connection string (postgresql://user:pass@host:port/db)
    pg_dump_path / pg_restore_path: executable paths (probed via shutil.which if omitted)
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
        """pg_dump backup to dest (.sql.gz, gzip compressed). Returns the output path.

        Fail-visible on failure: missing pg_dump or non-zero exit -> raise, never silently return an empty file.
        """
        if not self.pg_dump_path:
            raise RuntimeError("pg_dump not found — install postgresql-client or pass pg_dump_path explicitly")
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        sql_path = dest_path.with_suffix(".sql")
        logger.info(f"pg_dump backup -> {sql_path} (dsn={_safe_dsn(self.dsn)})")
        proc = subprocess.run(
            [self.pg_dump_path, "--no-owner", "--no-privileges", self.dsn],
            stdout=open(sql_path, "w"),
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            logger.error(f"pg_dump failed (exit={proc.returncode}): {proc.stderr[:500]}")
            raise RuntimeError(f"pg_dump failed: {proc.stderr[:200]}")
        with (
            open(sql_path, "rb") as fin,
            gzip.open(str(dest_path) if str(dest_path).endswith(".gz") else str(dest_path) + ".gz", "wb") as fout,
        ):
            shutil.copyfileobj(fin, fout)
        sql_path.unlink()
        gz_path = dest_path if str(dest_path).endswith(".gz") else dest_path.parent / (dest_path.name + ".gz")
        logger.info(f"backup complete: {gz_path} ({gz_path.stat().st_size} bytes)")
        return gz_path

    def restore(self, src: str | Path, confirm: bool = False) -> None:
        """Restore from a .sql.gz. Destructive operation — refuses to run with confirm=False.

        Runs the decompressed SQL via psql. Must be confirmed before restoring (overwrites existing data).
        """
        if not confirm:
            raise RuntimeError("restore is a destructive operation — pass confirm=True to confirm overwriting the existing database")
        if not self.pg_restore_path:
            raise RuntimeError("psql not found — install postgresql-client or pass pg_restore_path explicitly")
        src_path = Path(src)
        if not src_path.exists():
            raise FileNotFoundError(f"backup file not found: {src_path}")
        logger.warning(f"starting restore {src_path} -> {_safe_dsn(self.dsn)} (overwrites existing data!)")
        with gzip.open(str(src_path), "rb") as fin:
            proc = subprocess.run(
                [self.pg_restore_path, "-v", "--no-owner", self.dsn],
                stdin=fin,
                capture_output=True,
            )
        if proc.returncode != 0:
            logger.error(f"psql restore failed (exit={proc.returncode}): {proc.stderr.decode('utf-8', 'ignore')[:500]}")
            raise RuntimeError("psql restore failed — see logs")
        logger.info(f"restore complete: {src_path}")


def _safe_dsn(dsn: str) -> str:
    """dsn log redaction — hides the password."""
    import re

    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn)
