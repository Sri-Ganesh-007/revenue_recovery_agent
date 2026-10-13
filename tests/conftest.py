"""Safety guard for the whole test suite.

Automated tests must NEVER connect to the development database. This file runs before any test
module is imported and:
  1. forces DATABASE_URL to an in-memory SQLite URL, so importing the app can never pick up the
     development URL from .env (dotenv does not override variables that are already set);
  2. wraps sqlalchemy.create_engine so that creating an engine for the development database
     (matched by database name, on any host) raises immediately;
  3. keeps your two original scripts (test_repo.py, test_context.py) out of collection: they are print-style
     scripts that read the development database as soon as they are imported.

PostgreSQL integration tests use the `pg_test_url` fixture, which only accepts a database whose
name ends in "_test" and which is not the development database. Those tests must roll back
everything they do.
"""
import os
from pathlib import Path

import pytest
import sqlalchemy
import sqlalchemy.engine
from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parent.parent

# Never run these under pytest: they read the development database on import. (The files are left untouched.)
collect_ignore = ["test_repo.py", "test_context.py"]


def _development_database_name() -> str | None:
    url = dotenv_values(ROOT / ".env").get("DATABASE_URL")
    return make_url(url).database if url else None


DEV_DATABASE = _development_database_name()
os.environ["DATABASE_URL"] = "sqlite://"            # tests never inherit the development URL

_real_create_engine = sqlalchemy.create_engine


def guarded_create_engine(url, *args, **kwargs):
    target = url if isinstance(url, URL) else make_url(url)
    if DEV_DATABASE and target.get_backend_name() == "postgresql" and target.database == DEV_DATABASE:
        raise RuntimeError(f"Refusing to create an engine for the development database {DEV_DATABASE!r} in tests")
    return _real_create_engine(url, *args, **kwargs)


sqlalchemy.create_engine = guarded_create_engine
sqlalchemy.engine.create_engine = guarded_create_engine


def check_safe_test_url(test_url: str) -> str:
    """Accept only a PostgreSQL database named *_test that is not the development database."""
    target = make_url(test_url)
    if not (target.database or "").endswith("_test"):
        raise RuntimeError(f"Refusing: database {target.database!r} does not end in '_test'")
    if target.database == DEV_DATABASE:
        raise RuntimeError("Refusing: TEST_DATABASE_URL points at the development database")
    return test_url


@pytest.fixture(scope="session")
def pg_test_url():
    """URL of the separate PostgreSQL test database, or skip. Tests using it must roll back everything."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    return check_safe_test_url(url)
