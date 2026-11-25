import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed


@pytest.fixture(scope="module", autouse=True)
def seed_database():
    seed(reset=True)


def _signed_in_client(username="student", password="stud-dev"):
    client = TestClient(app)
    response = client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "access_token" in client.cookies
    return client


def test_student_listings_link_points_to_ssr_page():
    client = _signed_in_client()

    response = client.get("/app")

    assert response.status_code == 200
    assert 'href="/app/listings"' in response.text
    assert "/api/student/listings" not in response.text


def test_authenticated_user_can_access_listings_page():
    client = _signed_in_client()

    response = client.get("/app/listings")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Student listings" in response.text


def test_listings_page_renders_listing_fields():
    client = _signed_in_client()

    response = client.get("/app/listings")

    assert "Verified Active Flat" in response.text
    assert "st_augustine" in response.text
    assert "3500" in response.text
    assert "wifi,parking" in response.text
    assert "555-0100" in response.text
    assert "landlord@example.com" in response.text
    assert "Pending Unit" not in response.text


def test_listings_page_shows_empty_state_for_no_matches():
    client = _signed_in_client()

    response = client.get("/app/listings", params={"area": "unknown_area"})

    assert response.status_code == 200
    assert "No listings are available right now" in response.text


def test_non_student_receives_forbidden_page():
    client = _signed_in_client("landlord", "land-dev")

    response = client.get("/app/listings")

    assert response.status_code == 403
    assert "students only" in response.text


def test_unauthenticated_request_is_denied():
    response = TestClient(app).get("/app/listings", follow_redirects=False)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("text/html")
    assert "Sign in required" in response.text


def test_student_api_still_requires_bearer_authentication():
    client = TestClient(app)

    unauthenticated = client.get("/api/student/listings")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"detail": "Could not validate credentials"}

    browser_client = _signed_in_client()
    cookie_only = browser_client.get("/api/student/listings")
    assert cookie_only.status_code == 401
    assert cookie_only.json() == {"detail": "Could not validate credentials"}

    login = client.post(
        "/api/auth/login",
        json={"email": "student@example.com", "password": "stud-dev"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    bearer = client.get(
        "/api/student/listings",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 200
    assert isinstance(bearer.json()["listings"], list)
