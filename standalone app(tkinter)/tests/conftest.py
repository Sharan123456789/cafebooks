import pytest
from database.excel_manager import ExcelManager
from services.accounting_service import AccountingService
from services.reporting_service import ReportingService


@pytest.fixture
def db(tmp_path):
    return ExcelManager(tmp_path / "data", tmp_path / "backups")


@pytest.fixture
def svc(db):
    return AccountingService(db, actor="tester")


@pytest.fixture
def rep(svc):
    return ReportingService(svc)


@pytest.fixture
def vendor(svc):
    return svc.add_vendor("Fresh Milk Co", "Milk and Dairy")
