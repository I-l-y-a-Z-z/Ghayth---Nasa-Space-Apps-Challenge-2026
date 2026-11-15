import pytest
from fastapi.testclient import TestClient

from agrix.api import create_app
from agrix.demo import demo_plot


@pytest.fixture
def client():
    app = create_app("sqlite+pysqlite:///:memory:", initialize=True)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.state.engine.dispose()


@pytest.fixture
def plot(client):
    response = client.post("/plots", json=demo_plot().model_dump(mode="json"))
    assert response.status_code == 201
    return response.json()


@pytest.fixture
def evaluation_request(plot):
    return {
        "plot_id": plot["id"],
        "plot_revision": plot["revision"],
        "start_date": "2025-11-01",
        "horizon_days": 730,
        "sequence": [{"crop_id": "wheat_demo"}],
    }
