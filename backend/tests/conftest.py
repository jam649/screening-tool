import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app


@pytest.fixture
def config(tmp_path):
    dist = tmp_path / "frontend"
    dist.mkdir()
    (dist / "index.html").write_text("<html><body>screening-tool setup</body></html>")
    return Config(
        secret_key=Fernet.generate_key().decode(),
        scheduler_enabled=False,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        frontend_dist=dist,
        allowed_hosts=["testserver", "localhost", "127.0.0.1"],
    )


@pytest.fixture
def app(config):
    return create_app(config)


@pytest.fixture
def client(app):
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client


@pytest.fixture
def admin(client):
    response = client.post(
        "/api/v1/setup",
        json={
            "email": "admin@example.com",
            "display_name": "Administrator",
            "password": "test password with enough length",
            "disclaimer_acknowledged": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()
