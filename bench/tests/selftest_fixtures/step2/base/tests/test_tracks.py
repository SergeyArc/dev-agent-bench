from fastapi.testclient import TestClient

from tracksvc.api.app import app


def test_create_and_read() -> None:
    with TestClient(app) as client:
        created = client.post("/tracks", json={"code": "TRK-A1", "capacity": 100})
        read = client.get(f"/tracks/{created.json()['id']}")
    assert created.status_code == 201
    assert read.status_code == 200
    assert read.json() == created.json()


def test_duplicate_code() -> None:
    with TestClient(app) as client:
        client.post("/tracks", json={"code": "TRK-A1", "capacity": 100})
        second = client.post("/tracks", json={"code": "TRK-A1", "capacity": 5})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "track_code_taken"
