"""issue #221 — DB conventions (architecture/fusion-db-conventions-1030.md).

- DB-2/DB-3: DATABASE_URL 为唯一规范 Postgres 连接名 (FUSION_PG_DSN 旧名仍接受)
- DB-4: .env.example 占位无凭据
- DB-9: 连接时幂等建 schema (live 集成测试门控 env)
- SpaceStore __init__ 无 I/O → env 决议可纯单元测; SessionStore 经 monkeypatch _init_postgres 测决议
"""

import os
from pathlib import Path

import pytest

from fusion_cowork.engine import session as session_mod
from fusion_cowork.engine.session import SessionStore
from fusion_cowork.space import store as store_mod
from fusion_cowork.space.store import SpaceStore

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
README = REPO_ROOT / "README.md"


def _clear_dsn_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FUSION_PG_DSN", raising=False)


class TestSpaceStoreEnvResolution:
    def test_database_url_selects_postgres(self, monkeypatch, tmp_path):
        _clear_dsn_env(monkeypatch)
        monkeypatch.setenv("DATABASE_URL", "postgresql://127.0.0.1:5432/fusion_cowork")
        s = SpaceStore(data_dir=str(tmp_path))
        assert s.backend == "postgres"
        assert s._dsn == "postgresql://127.0.0.1:5432/fusion_cowork"

    def test_legacy_fusion_pg_dsn_still_honored(self, monkeypatch, tmp_path):
        _clear_dsn_env(monkeypatch)
        monkeypatch.setenv("FUSION_PG_DSN", "postgresql://127.0.0.1:5432/legacy")
        s = SpaceStore(data_dir=str(tmp_path))
        assert s.backend == "postgres"
        assert s._dsn == "postgresql://127.0.0.1:5432/legacy"

    def test_database_url_wins_over_legacy(self, monkeypatch, tmp_path):
        _clear_dsn_env(monkeypatch)
        monkeypatch.setenv("DATABASE_URL", "postgresql://127.0.0.1:5432/canonical")
        monkeypatch.setenv("FUSION_PG_DSN", "postgresql://127.0.0.1:5432/legacy")
        s = SpaceStore(data_dir=str(tmp_path))
        assert s._dsn == "postgresql://127.0.0.1:5432/canonical"

    def test_dsn_arg_wins_over_env(self, monkeypatch, tmp_path):
        _clear_dsn_env(monkeypatch)
        monkeypatch.setenv("DATABASE_URL", "postgresql://127.0.0.1:5432/env")
        s = SpaceStore(data_dir=str(tmp_path), dsn="postgresql://127.0.0.1:5432/arg")
        assert s._dsn == "postgresql://127.0.0.1:5432/arg"

    def test_no_env_no_dsn_selects_sqlite(self, monkeypatch, tmp_path):
        _clear_dsn_env(monkeypatch)
        s = SpaceStore(data_dir=str(tmp_path))
        assert s.backend == "sqlite"

    def test_env_names_declared_canonical(self):
        assert store_mod._PG_DSN_ENV == "DATABASE_URL"
        assert store_mod._PG_DSN_ENV_LEGACY == "FUSION_PG_DSN"


class TestSessionStoreEnvResolution:
    def test_database_url_selects_postgres(self, monkeypatch, tmp_path):
        _clear_dsn_env(monkeypatch)
        monkeypatch.setattr(SessionStore, "_init_postgres", lambda self: None)
        monkeypatch.setenv("DATABASE_URL", "postgresql://127.0.0.1:5432/fusion_cowork")
        st = SessionStore(db_path=str(tmp_path / "sessions.db"))
        assert st._backend == "postgres"

    def test_legacy_env_and_precedence(self, monkeypatch, tmp_path):
        _clear_dsn_env(monkeypatch)
        monkeypatch.setattr(SessionStore, "_init_postgres", lambda self: None)
        monkeypatch.setenv("FUSION_PG_DSN", "postgresql://127.0.0.1:5432/legacy")
        assert SessionStore(db_path=str(tmp_path / "s.db"))._backend == "postgres"
        monkeypatch.setenv("DATABASE_URL", "postgresql://127.0.0.1:5432/canonical")
        assert SessionStore(db_path=str(tmp_path / "s2.db"))._dsn == "postgresql://127.0.0.1:5432/canonical"

    def test_no_env_selects_sqlite(self, monkeypatch, tmp_path):
        _clear_dsn_env(monkeypatch)
        st = SessionStore(db_path=str(tmp_path / "sessions.db"))
        assert st._backend == "sqlite"

    def test_env_names_declared_canonical(self):
        assert session_mod._PG_DSN_ENV == "DATABASE_URL"
        assert session_mod._PG_DSN_ENV_LEGACY == "FUSION_PG_DSN"


class TestCliDbCommands:
    def test_db_migrate_missing_dsn_error_mentions_database_url(self, monkeypatch):
        _clear_dsn_env(monkeypatch)
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        result = CliRunner().invoke(cli, ["db", "migrate"])
        assert result.exit_code != 0
        assert "DATABASE_URL" in result.output

    def test_db_backup_help_mentions_database_url(self):
        from click.testing import CliRunner

        from fusion_cowork.cli import cli

        result = CliRunner().invoke(cli, ["db", "backup", "--help"])
        assert result.exit_code == 0
        assert "DATABASE_URL" in result.output


class TestEnvExampleContract:
    def test_contains_canonical_urls(self):
        text = ENV_EXAMPLE.read_text()
        assert "DATABASE_URL=postgresql://127.0.0.1:5432/fusion_cowork" in text
        assert "REDIS_URL=redis://127.0.0.1:6379/1" in text

    def test_createdb_first_run_note(self):
        text = ENV_EXAMPLE.read_text()
        assert "createdb fusion_cowork" in text

    def test_no_credentials(self):
        text = ENV_EXAMPLE.read_text()
        for line in text.splitlines():
            if "://" in line and "=" in line and not line.strip().startswith("#"):
                assert "@" not in line, f"credential-like URL in .env.example: {line}"


class TestReadmeContract:
    def test_readme_documents_database_url_and_createdb(self):
        text = README.read_text()
        assert "DATABASE_URL" in text
        assert "postgresql://127.0.0.1:5432/fusion_cowork" in text
        assert "createdb fusion_cowork" in text
        assert "REDIS_URL" in text


try:
    import asyncpg  # noqa: F401

    _HAS_ASYNCPG = True
except ImportError:
    _HAS_ASYNCPG = False

_LIVE_DSN = os.environ.get("DATABASE_URL") or os.environ.get("FUSION_PG_DSN")


@pytest.mark.asyncio
@pytest.mark.postgres
@pytest.mark.skipif(
    not (_HAS_ASYNCPG and _LIVE_DSN),
    reason="DATABASE_URL 未设 (或无 asyncpg) — 跳过 live Postgres 集成 (issue #221 验收)",
)
class TestLivePostgresIdempotentSchema:
    async def test_initialize_idempotent_and_health(self):
        s = SpaceStore(dsn=_LIVE_DSN)
        assert s.backend == "postgres"
        await s.initialize()
        try:
            spaces = await s.list_spaces()
            assert isinstance(spaces, list)
        finally:
            await s.close()
        # DB-9: 二次 initialize (同 schema) 幂等
        s2 = SpaceStore(dsn=_LIVE_DSN)
        await s2.initialize()
        await s2.close()
