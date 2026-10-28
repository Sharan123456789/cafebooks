"""Every storage-backed test runs against SQLite and, when available, PostgreSQL.

PostgreSQL comes from CAFEBOOKS_TEST_POSTGRES_URL (an admin URL to any server), or from the
optional `pgserver` package, which starts a throwaway local server.  Without either, the
PostgreSQL variants are skipped.
"""
import os
import uuid

import pytest
import sqlalchemy as sa

from cafebooks.database.sql_store import SqlStore, normalize_url
from cafebooks.services.accounting_service import AccountingService
from cafebooks.services.reporting_service import ReportingService

_PG = {"url": None, "server": None, "tried": False}


def _postgres_admin_url(tmp_root):
    if not _PG["tried"]:
        _PG["tried"] = True
        url = os.environ.get("CAFEBOOKS_TEST_POSTGRES_URL", "").strip()
        if not url:
            try:
                import pgserver
                _PG["server"] = pgserver.get_server(str(tmp_root / "pgdata"), cleanup_mode="stop")
                url = _PG["server"].get_uri()
            except Exception:  # noqa: BLE001
                url = None
        _PG["url"] = url
    return _PG["url"]


@pytest.fixture(scope="session")
def _pg_root(tmp_path_factory):
    return tmp_path_factory.mktemp("pg")


@pytest.fixture(params=["sqlite", "postgresql"])
def db_url(request, tmp_path, _pg_root):
    if request.param == "sqlite":
        yield f"sqlite:///{(tmp_path / 'cafebooks.db').as_posix()}"
        return
    admin = _postgres_admin_url(_pg_root)
    if not admin:
        pytest.skip("PostgreSQL not available (set CAFEBOOKS_TEST_POSTGRES_URL or install pgserver)")
    name = f"cb_test_{uuid.uuid4().hex[:12]}"
    engine = sa.create_engine(normalize_url(admin), isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        c.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = sa.engine.make_url(normalize_url(admin)).set(database=name).render_as_string(hide_password=False)
    yield url
    with engine.connect() as c:
        c.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    engine.dispose()


@pytest.fixture
def make_db(db_url, tmp_path):
    stores = []

    def make(**kw):
        kw.setdefault("backup_dir", tmp_path / "backups")
        store = SqlStore(db_url, **kw)
        stores.append(store)
        return store
    yield make
    for s in stores:
        s.close()


@pytest.fixture
def db(make_db):
    return make_db()


@pytest.fixture
def svc(db):
    return AccountingService(db, actor="tester")


@pytest.fixture
def rep(svc):
    return ReportingService(svc)


@pytest.fixture
def vendor(svc):
    return svc.add_vendor("Fresh Milk Co", "Milk and Dairy")
