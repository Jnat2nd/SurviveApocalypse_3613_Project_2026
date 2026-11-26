"""Shared pytest configuration for the whole suite.

Every database-backed component is redirected to an in-memory SQLite engine
before any test request is made, so no test can read or write the repository's
``database.db`` file.  The application modules bind ``engine`` at import time;
each of those bindings is replaced here.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.database as database_module
import app.models
import app.seed as seed_module
from app.database import get_session
from app.main import app
from app.seed import seed
from app.services import housing_auth, listings

TEST_ENGINE = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

database_module.engine = TEST_ENGINE
seed_module.engine = TEST_ENGINE
housing_auth.engine = TEST_ENGINE
listings.engine = TEST_ENGINE


SEED_ACCOUNTS = {
    "admin": ("admin@example.com", "admin-dev"),
    "student": ("student@example.com", "stud-dev"),
    "incoming": ("incoming@example.com", "stud-dev"),
    "landlord": ("landlord@example.com", "land-dev"),
    "landlord2": ("landlord2@example.com", "land-dev"),
}


@pytest.fixture
def engine():
    """A fresh, empty in-memory database for each test."""
    SQLModel.metadata.drop_all(TEST_ENGINE)
    SQLModel.metadata.create_all(TEST_ENGINE)
    return TEST_ENGINE


@pytest.fixture
def db_session(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def test_session(db_session):
    return db_session


@pytest.fixture
def seeded_session(engine):
    seed(reset=True)
    with Session(engine) as session:
        yield session


@pytest.fixture
def persistence_session():
    with Session(TEST_ENGINE) as session:
        yield session


class ApiActions:
    """Small helper for HTTP-level scenario and integration tests."""

    def __init__(self, client):
        self.client = client

    def login(self, email, password):
        return self.client.post(
            "/api/auth/login", json={"email": email, "password": password}
        )

    def headers(self, email, password):
        response = self.login(email, password)
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def headers_for(self, role):
        email, password = SEED_ACCOUNTS[role]
        return self.headers(email, password)

    def user_id(self, email, password):
        response = self.login(email, password)
        assert response.status_code == 200, response.text
        return response.json()["user"]["id"]

    def submit(self, title="Helper Unit", *, headers=None, **overrides):
        payload = {
            "title": title,
            "area": "st_augustine",
            "price": 2000,
            "amenities": "wifi",
            "contact_phone": "555-0200",
            "contact_email": "landlord@example.com",
        }
        payload.update(overrides)
        response = self.client.post(
            "/api/landlord/listings",
            json=payload,
            headers=headers if headers is not None else self.headers_for("landlord"),
        )
        assert response.status_code == 200, response.text
        return response.json()["listing"]

    def transition(self, listing_id, action, headers):
        return self.client.post(
            f"/api/listings/{listing_id}/transition",
            json={"action": action},
            headers=headers,
        )

    def admin_listing(self, title, status=None):
        params = {"status": status} if status else None
        response = self.client.get(
            "/api/admin/listings", params=params, headers=self.headers_for("admin")
        )
        assert response.status_code == 200, response.text
        return next(
            item for item in response.json()["listings"] if item["title"] == title
        )

    def status(self, listing_id):
        response = self.client.get(
            "/api/admin/listings", headers=self.headers_for("admin")
        )
        assert response.status_code == 200, response.text
        return next(
            item["status"]
            for item in response.json()["listings"]
            if item["id"] == listing_id
        )

    def browse(self, role, **params):
        response = self.client.get(
            "/api/student/listings", params=params, headers=self.headers_for(role)
        )
        assert response.status_code == 200, response.text
        return response.json()["listings"]


@pytest.fixture
def api(client):
    return ApiActions(client)


@pytest.fixture
def client(engine):
    """A TestClient backed exclusively by the isolated in-memory database."""
    seed(reset=True)

    def override_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
