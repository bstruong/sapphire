"""Pytest fixtures shared across the SAPPHIRE test suite."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
from psycopg import sql

from app.db.connection import DEFAULT_TEST_DATABASE_URL, init_db
from app.models.canonical import RawStatement

logger = logging.getLogger(__name__)
FIXTURES_DIR: Path = Path(__file__).parent / "fixtures"
_SAFE_DB_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_test_database_url(url: str) -> bool:
    """Reject dogfood/runtime DB URLs so pytest cannot truncate Vault data."""
    name = urlparse(url).path.lstrip("/").lower()
    return name.endswith(("_test", "_pytest"))


def _replace_database_name(url: str, database: str) -> str:
    if not _SAFE_DB_NAME.fullmatch(database):
        raise ValueError(f"Unsafe database name for pytest: {database!r}")
    parts = urlparse(url)
    return urlunparse(parts._replace(path=f"/{database}"))


def _admin_url(url: str) -> str:
    return _replace_database_name(url, "postgres")


def _ensure_worker_database(base_url: str, worker_id: str) -> str:
    """Return a per-xdist-worker DB URL; create the DB if needed.

    Without xdist, ``worker_id`` is ``master`` and the base test DB is reused.
    With xdist, each ``gwN`` worker gets ``{base}_{gwN}`` so TRUNCATE cannot
    race across workers (coverage gate still runs on the full suite).
    """
    parts = urlparse(base_url)
    base_name = parts.path.lstrip("/")
    if not base_name:
        raise ValueError("TEST database URL must include a database name")
    if worker_id == "master":
        return base_url

    worker_db = f"{base_name}_{worker_id}"
    if not _SAFE_DB_NAME.fullmatch(worker_db):
        raise ValueError(f"Unsafe worker database name: {worker_db!r}")

    worker_url = _replace_database_name(base_url, worker_db)
    with psycopg.connect(
        _admin_url(base_url), connect_timeout=5, autocommit=True
    ) as admin:
        row = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (worker_db,),
        ).fetchone()
        if row is None:
            admin.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(worker_db))
            )
            logger.info(
                "Created per-worker test database", extra={"database": worker_db}
            )
    return worker_url


@pytest.fixture()
def schwab_sample_path() -> Path:
    """Return the absolute path to the Schwab sample JSON fixture."""
    path: Path = FIXTURES_DIR / "schwab_sample.json"
    assert path.is_file(), f"Fixture missing: {path}"
    return path


@pytest.fixture()
def schwab_raw_statement(schwab_sample_path: Path) -> RawStatement:
    """Load and parse the Schwab fixture into a ``RawStatement``.

    Uses ``model_validate_json`` so that Pydantic's native JSON parser
    handles ``str → date`` and ``str → enum`` coercion correctly under
    strict mode.
    """
    raw_json: str = schwab_sample_path.read_text(encoding="utf-8")
    return RawStatement.model_validate_json(raw_json)


@pytest.fixture(scope="session")
def worker_id() -> str:
    """xdist worker id (``gw0``…) or ``master`` when running without ``-n``.

    Uses ``PYTEST_XDIST_WORKER`` so we do not depend on the xdist plugin
    fixture (still works if the plugin is disabled).
    """
    return os.environ.get("PYTEST_XDIST_WORKER", "master")


@pytest.fixture(scope="session")
def postgres_url(worker_id: str) -> str:
    """Resolve an isolated PostgreSQL URL for this pytest worker only.

    Never falls back to ``DATABASE_URL`` (dogfood/Vault). Only
    ``TEST_DATABASE_URL`` or known ``*_test`` defaults are considered.
    """
    candidate_urls = [
        os.environ.get("TEST_DATABASE_URL"),
        DEFAULT_TEST_DATABASE_URL,
        "postgresql://postgres:postgres@localhost:5432/sapphire_test",
        "postgresql://postgres:assassin@localhost:54329/assassin_test",
        "postgresql://postgres:postgres@localhost:5432/assassin_test",
    ]
    base_url: str | None = None
    for url in candidate_urls:
        if not url:
            continue
        if not _is_test_database_url(url):
            logger.warning(
                "Ignoring non-test database URL for pytest: %s",
                urlparse(url).path,
            )
            continue
        try:
            with psycopg.connect(url, connect_timeout=1) as conn:
                conn.execute("SELECT 1;")
                base_url = url
                break
        except psycopg.Error as exc:
            logger.debug("Failed connecting to candidate %s: %s", url, exc)

    if base_url is None and shutil.which("docker"):
        try:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--name",
                    "sapphire-test-db",
                    "-e",
                    "POSTGRES_PASSWORD=sapphire",
                    "-e",
                    "POSTGRES_DB=sapphire_test",
                    "-p",
                    "54329:5432",
                    "-d",
                    "postgres:16",
                ],
                capture_output=True,
                check=False,
            )
            container_url = DEFAULT_TEST_DATABASE_URL
            admin_url = "postgresql://postgres:sapphire@localhost:54329/postgres"
            for _ in range(20):
                time.sleep(0.5)
                try:
                    with psycopg.connect(container_url, connect_timeout=1) as conn:
                        conn.execute("SELECT 1;")
                    with psycopg.connect(
                        admin_url, connect_timeout=1, autocommit=True
                    ) as admin:
                        row = admin.execute(
                            "SELECT 1 FROM pg_database WHERE datname = 'sapphire'"
                        ).fetchone()
                        if row is None:
                            admin.execute("CREATE DATABASE sapphire")
                    base_url = container_url
                    break
                except psycopg.Error as exc:
                    logger.debug("Waiting for postgres container: %s", exc)
        except OSError as exc:
            logger.debug("Docker execution failed: %s", exc)

    if base_url is None:
        pytest.skip("PostgreSQL test database is not accessible")

    return _ensure_worker_database(base_url, worker_id)


@pytest.fixture(scope="session", autouse=True)
def _isolate_app_database_url(postgres_url: str) -> Iterator[None]:
    """Point the test process DATABASE_URL at sapphire_test only.

    Leaves the caller's shell / uvicorn env untouched (separate process).
    """
    previous_database_url = os.environ.get("DATABASE_URL")
    previous_test_url = os.environ.get("TEST_DATABASE_URL")
    os.environ["TEST_DATABASE_URL"] = postgres_url
    os.environ["DATABASE_URL"] = postgres_url
    try:
        yield
    finally:
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        if previous_test_url is None:
            os.environ.pop("TEST_DATABASE_URL", None)
        else:
            os.environ["TEST_DATABASE_URL"] = previous_test_url


@pytest.fixture(scope="session", autouse=True)
def _session_schema(postgres_url: str) -> None:
    """Apply migrations once per worker (not once per test).

    Speeds CI without dropping coverage: same suite, same schema, less setup.
    """
    with psycopg.connect(postgres_url) as conn:
        init_db(conn)
        conn.commit()


_TRUNCATE_LEDGER_SQL = """
TRUNCATE TABLE spend_label_suggestions, spend_label_rules,
                           transaction_spend_labels, envelopes, raw_tokens, raw_pages,
               transactions, holdings,
               statement_depository_summaries, statement_credit_summaries,
               statement_brokerage_summaries, statements, accounts,
               extraction_runs, raw_payloads CASCADE;
"""


@pytest.fixture()
def db_conn(postgres_url: str) -> Iterator[psycopg.Connection]:
    """Yield a connection; truncate tables before and after each test."""
    conn = psycopg.connect(postgres_url)
    try:
        try:
            conn.execute(_TRUNCATE_LEDGER_SQL)
            conn.commit()
        except psycopg.Error as exc:
            logger.debug("Setup TRUNCATE failed: %s", exc)
            conn.rollback()
        yield conn
    finally:
        try:
            conn.execute(_TRUNCATE_LEDGER_SQL)
            conn.commit()
        except psycopg.Error as exc:
            logger.debug("Cleanup TRUNCATE failed: %s", exc)
        conn.close()
