import os
import tempfile

# Must be set before syncwatch is imported: settings are read once at import time.
os.environ["SYNCWATCH_DB"] = os.path.join(tempfile.mkdtemp(prefix="syncwatch-test-"), "test.db")
os.environ["RETRY_BASE_SECONDS"] = "0"
os.environ["RETRY_MAX_WAIT_SECONDS"] = "0"
os.environ.pop("ALERT_WEBHOOK_URL", None)

import pytest
from fastapi.testclient import TestClient

from syncwatch import seed
from syncwatch.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def fresh_data(client):
    seed.reset()
    yield


@pytest.fixture
def scenario(client):
    def _set(name, enabled=True):
        r = client.put(f"/api/scenarios/{name}", json={"enabled": enabled})
        assert r.status_code == 200
    return _set
