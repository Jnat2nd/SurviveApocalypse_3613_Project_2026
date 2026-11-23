"""Shallow scenario smoke tests — intentionally weak."""

from fastapi.testclient import TestClient

from app.main import app
from app.seed import seed


def setup_module() -> None:
    seed(reset=True)


client = TestClient(app)


def _user_id(email, password):
    response = client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200
    return response.json()["user"]["id"]


def _headers_for(email, password):
    return {"X-User-Id": str(_user_id(email, password))}


def _admin_headers():
    return _headers_for("admin@example.com", "admin-dev")


def _admin_listing(title):
    response = client.get("/api/admin/listings", headers=_admin_headers())
    assert response.status_code == 200
    return next(
        listing
        for listing in response.json()["listings"]
        if listing["title"] == title
    )


def test_health_ok():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["scenario"] == "student-accommodation"


def test_login_admin():
    r = client.post(
        "/api/auth/login",
        json={"email": "admin@example.com", "password": "admin-dev"},
    )
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "admin"
    assert "password" not in r.json()["user"]


def test_student_browse():
    login = client.post(
        "/api/auth/login",
        json={"email": "student@example.com", "password": "stud-dev"},
    )
    uid = login.json()["user"]["id"]
    r = client.get("/api/student/listings", headers={"X-User-Id": str(uid)})
    assert r.status_code == 200
    assert isinstance(r.json()["listings"], list)


def test_student_browse_visibility_follows_status_only():
    headers = _headers_for("student@example.com", "stud-dev")

    response = client.get("/api/student/listings", headers=headers)

    assert response.status_code == 200
    titles = [listing["title"] for listing in response.json()["listings"]]
    assert "Verified Active Flat" in titles
    assert "Pending Unit" not in titles


def test_transition_rejects_illegal_activation_from_pending():
    pending = _admin_listing("Pending Unit")

    response = client.post(
        f"/api/listings/{pending['id']}/transition",
        json={"action": "activate"},
        headers=_admin_headers(),
    )

    assert response.status_code == 400
    assert "invalid transition" in response.json()["detail"]


def test_student_cannot_message_pending_listing():
    pending = _admin_listing("Pending Unit")
    headers = _headers_for("student@example.com", "stud-dev")

    response = client.post(
        "/api/student/messages",
        json={"listing_id": pending["id"], "body": "Is this still available?"},
        headers=headers,
    )

    assert response.status_code == 400
    assert "not available for messaging" in response.json()["detail"]


def test_admin_filter_accepts_approved_and_legacy_verified():
    landlord_headers = _headers_for("landlord@example.com", "land-dev")
    submit_response = client.post(
        "/api/landlord/listings",
        json={
            "title": "Approval Filter Unit",
            "area": "st_augustine",
            "price": 2500,
            "amenities": "wifi",
            "contact_phone": "555-0199",
            "contact_email": "landlord@example.com",
            "photos": "",
        },
        headers=landlord_headers,
    )
    assert submit_response.status_code == 200
    listing = submit_response.json()["listing"]

    transition_response = client.post(
        f"/api/listings/{listing['id']}/transition",
        json={"action": "verify"},
        headers=_admin_headers(),
    )
    assert transition_response.status_code == 200
    assert transition_response.json()["listing"]["status"] == "approved"

    for requested_status in ("approved", "verified"):
        response = client.get(
            "/api/admin/listings",
            params={"status": requested_status},
            headers=_admin_headers(),
        )
        assert response.status_code == 200
        assert any(
            item["id"] == listing["id"] for item in response.json()["listings"]
        )
