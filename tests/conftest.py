"""Shared fixtures. DB-backed tests are skipped unless SAFESQL_DATABASE_URL is reachable."""

import os

import psycopg
import pytest

from safesql.config import Settings
from safesql.executor import Executor
from safesql.service import SafeSQLService

DB_URL = os.environ.get("SAFESQL_DATABASE_URL")


def _reachable() -> bool:
    if not DB_URL:
        return False
    try:
        psycopg.connect(DB_URL, connect_timeout=3).close()
        return True
    except psycopg.Error:
        return False


requires_db = pytest.mark.skipif(not _reachable(), reason="needs SAFESQL_DATABASE_URL")


@pytest.fixture(scope="session")
def executor():
    ex = Executor(DB_URL, statement_timeout_ms=1500, max_cost=50_000)
    yield ex
    ex.close()


@pytest.fixture(scope="session")
def concert(executor):
    return SafeSQLService(Settings(database_url=DB_URL).with_schemas("spider_concert_singer"), executor)


@pytest.fixture(scope="session")
def shop(executor):
    return SafeSQLService(Settings(database_url=DB_URL).with_schemas("inject_demo"), executor)
