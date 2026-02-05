"""Tests for /stats endpoint."""

from fastapi.testclient import TestClient

from api import app

client = TestClient(app)


def test_stats_endpoint_returns_counts():
    """Test /stats returns agent counts, market count, and volume."""
    response = client.get("/stats")
    assert response.status_code == 200
    data = response.json()

    # Check structure
    assert "agents" in data
    assert "markets" in data
    assert "volume" in data
    assert "currency" in data

    # Check agents breakdown
    agents = data["agents"]
    assert "total" in agents
    assert "claimed" in agents
    assert "pending" in agents
    assert agents["total"] >= 0
    assert agents["claimed"] >= 0
    assert agents["pending"] >= 0
    assert agents["total"] >= agents["claimed"] + agents["pending"]

    # Check types
    assert isinstance(data["markets"], int)
    assert isinstance(data["volume"], (int, float))
    assert data["currency"] == "ŧ"


def test_stats_endpoint_no_auth_required():
    """Test /stats is publicly accessible without authentication."""
    response = client.get("/stats")
    assert response.status_code == 200
