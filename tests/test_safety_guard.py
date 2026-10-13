"""The test-suite guard itself (see conftest.py). No database is contacted."""
import os

import pytest
import sqlalchemy
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

from tests import conftest

dev_url = dotenv_values(conftest.ROOT / ".env").get("DATABASE_URL")
needs_dev_url = pytest.mark.skipif(not dev_url, reason=".env has no DATABASE_URL")


def test_database_url_is_forced_to_in_memory_sqlite():
    assert os.environ["DATABASE_URL"] == "sqlite://"


@needs_dev_url
def test_creating_an_engine_for_the_development_database_is_refused():
    with pytest.raises(RuntimeError, match="development database"):
        sqlalchemy.create_engine(dev_url)


@needs_dev_url
def test_the_development_database_is_refused_on_any_host():
    other_host = make_url(dev_url).set(host="127.0.0.1", port=5999, username="someone")
    with pytest.raises(RuntimeError):
        sqlalchemy.create_engine(other_host)


def test_other_databases_are_allowed():
    assert sqlalchemy.create_engine("sqlite://") is not None
    assert sqlalchemy.create_engine("postgresql+psycopg2://u:p@localhost:5433/anything_test") is not None   # lazy: no connection


@needs_dev_url
def test_test_url_must_end_in_test_and_must_not_be_the_dev_database():
    dev = make_url(dev_url)
    with pytest.raises(RuntimeError):
        conftest.check_safe_test_url(dev.render_as_string(hide_password=False))
    with pytest.raises(RuntimeError):
        conftest.check_safe_test_url(dev.set(database="revenue_recovery_staging").render_as_string(hide_password=False))
    ok = dev.set(database="revenue_recovery_test").render_as_string(hide_password=False)
    assert conftest.check_safe_test_url(ok) == ok


def test_the_original_scripts_that_read_the_dev_database_are_not_collected():
    assert set(conftest.collect_ignore) == {"test_repo.py", "test_context.py"}


def test_pg_fixture_skips_without_a_url(pg_test_url):
    assert pg_test_url.endswith("_test") or "_test" in pg_test_url      # only reached when TEST_DATABASE_URL is set safely
