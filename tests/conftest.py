import pytest

from smartcity.config import Config
from smartcity.data import DATA_DIR, build_city


def pytest_configure(config):
    config.addinivalue_line("markers", "iasi: needs the committed Iași dataset in data/")


@pytest.fixture(scope="session")
def iasi_net():
    if not (DATA_DIR / "iasi_core.json").exists():
        pytest.skip("Iași dataset not built (run scripts/fetch_iasi.py)")
    return build_city("iasi", Config())


@pytest.fixture
def grid_net():
    return build_city("grid", Config())
