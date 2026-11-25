"""Integration tests for repositories, HTTP API auth, and transition flows."""

import jwt
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.repositories.user import UserRepository
from app.seed import seed
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.utilities.security import verify_password


def setup_module() -> None:
    seed(reset=True)


client = TestClient(app)


def _user_id(email, password):
    response = client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200
    return response.json()["user"]["id"]


def _headers_for(email, password):
    return {"X-User-Id": str(_user_id(email, password))}


def _bearer_headers(email, password):
    response = client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _admin_headers():
    return _bearer_headers("admin@example.com", "admin-dev")


def _owner_headers():
    return _bearer_headers("landlord@example.com", "land-dev")


def _other_landlord_headers():
    return _bearer_headers("landlord2@example.com", "land-dev")


def _student_headers():
    return _bearer_headers("student@example.com", "stud-dev")


def _admin_listing(title):
    response = client.get("/api/admin/listings", headers=_admin_headers())
    assert response.status_code == 200
    return next(
        listing
        for listing in response.json()["listings"]
        if listing["title"] == title
    )


def _submit_listing(title):
    response = client.post(
        "/api/landlord/listings",
        json={
            "title": title,
            "area": "st_augustine",
            "price": 2000,
            "amenities": "wifi",
            "contact_phone": "555-0200",
            "contact_email": "landlord@example.com",
        },
        headers=_owner_headers(),
    )
    assert response.status_code == 200
    return response.json()["listing"]


def _transition(listing_id, action, headers):
    return client.post(
        f"/api/listings/{listing_id}/transition",
        json={"action": action},
        headers=headers,
    )


def _status(listing_id):
    response = client.get("/api/admin/listings", headers=_admin_headers())
    assert response.status_code == 200
    return next(
        item["status"]
        for item in response.json()["listings"]
        if item["id"] == listing_id
    )


def test_save_password(test_session):
    repository = UserRepository(test_session)
    service = AuthService(repository)

    created = service.register_user("alice", "alice@example.com", "secret")
    saved = repository.get_by_username("alice")

    assert saved is not None
    assert created.id == saved.id
    assert saved.username == "alice"
    assert saved.email == "alice@example.com"
    assert saved.role == "regular_user"
    assert saved.password != "secret"
    assert verify_password("secret", saved.password)


def test_login(test_session):
    repository = UserRepository(test_session)
    service = AuthService(repository)
    service.register_user("alice", "alice@example.com", "secret")

    token = service.authenticate_user("alice", "secret")
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
    )

    assert payload["sub"] == str(repository.get_by_username("alice").id)
    assert payload["role"] == "regular_user"


def test_register_user(test_session):
    repository = UserRepository(test_session)
    auth_service = AuthService(repository)
    auth_service.register_user("alice", "alice@example.com", "secret")

    users = UserService(repository).get_all_users()

    assert len(users) == 1
    assert users[0].username == "alice"
    assert users[0].email == "alice@example.com"


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

    response = _transition(pending["id"], "activate", _admin_headers())

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

    transition_response = _transition(listing["id"], "verify", _admin_headers())
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


def test_login_returns_verifiable_bearer_token():
    response = client.post(
        "/api/auth/login",
        json={"email": "student@example.com", "password": "stud-dev"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    settings = get_settings()
    payload = jwt.decode(
        body["access_token"],
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
    )
    assert payload["sub"] == str(body["user"]["id"])
    assert payload["role"] == body["user"]["role"]
    assert "exp" in payload


def test_login_rejects_bad_credentials_with_401_json():
    response = client.post(
        "/api/auth/login",
        json={"email": "student@example.com", "password": "wrong"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}


def test_bearer_token_authenticates_protected_api_routes():
    student_headers = _bearer_headers("student@example.com", "stud-dev")
    landlord_headers = _bearer_headers("landlord@example.com", "land-dev")
    admin_headers = _bearer_headers("admin@example.com", "admin-dev")

    assert client.get("/api/student/listings", headers=student_headers).status_code == 200
    assert client.get("/api/users", headers=admin_headers).status_code == 200
    assert client.get("/api/admin/listings", headers=admin_headers).status_code == 200

    submitted = client.post(
        "/api/landlord/listings",
        json={
            "title": "Bearer Auth Unit",
            "area": "st_augustine",
            "price": 1200,
            "amenities": "wifi",
            "contact_phone": "555-0123",
            "contact_email": "landlord@example.com",
        },
        headers=landlord_headers,
    )
    assert submitted.status_code == 200
    assert submitted.json()["listing"]["landlord_id"] is not None


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("student@example.com", "stud-dev"),
        ("landlord@example.com", "land-dev"),
    ],
)
def test_non_admin_bearer_token_cannot_use_admin_routes(email, password):
    headers = _bearer_headers(email, password)

    for path in ("/api/users", "/api/admin/listings"):
        response = client.get(path, headers=headers)
        assert response.status_code == 403
        assert response.json()["detail"] == "admin only"


def test_api_without_credentials_returns_401_json_with_bearer_challenge():
    response = client.get("/api/student/listings")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Could not validate credentials"}


def test_api_with_invalid_bearer_token_returns_401_json():
    response = client.get(
        "/api/student/listings", headers={"Authorization": "Bearer invalid-token"}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials"


def test_non_api_401_still_renders_sign_in_page():
    response = client.get("/app")

    assert response.status_code == 401
    assert "Sign in required" in response.text
    assert "Return to sign in" in response.text


@pytest.mark.skipif(
    get_settings().env.lower() not in {"dev", "development"},
    reason="legacy X-User-Id fallback is development-only",
)
def test_legacy_header_still_authenticates_end_to_end_in_development():
    login = client.post(
        "/api/auth/login",
        json={"email": "student@example.com", "password": "stud-dev"},
    )
    user_id = login.json()["user"]["id"]

    response = client.get(
        "/api/student/listings", headers={"X-User-Id": str(user_id)}
    )

    assert response.status_code == 200
    assert "listings" in response.json()


def test_owner_submits_admin_verifies_then_owner_activates():
    listing = _submit_listing("Happy Path Unit")

    verified = _transition(listing["id"], "verify", _admin_headers())
    assert verified.status_code == 200
    assert verified.json()["listing"]["status"] == "approved"
    assert _status(listing["id"]) == "approved"

    activated = _transition(listing["id"], "activate", _owner_headers())
    assert activated.status_code == 200
    assert activated.json()["listing"]["status"] == "active"
    assert _status(listing["id"]) == "active"


def test_non_admin_cannot_verify_and_state_is_unchanged():
    listing = _submit_listing("Verify Gate Unit")

    response = _transition(listing["id"], "verify", _owner_headers())

    assert response.status_code == 403
    assert response.json()["detail"] == "admin only"
    assert _status(listing["id"]) == "pending"

    assert _transition(listing["id"], "verify", _admin_headers()).status_code == 200


def test_other_landlord_cannot_activate_owned_listing():
    listing = _submit_listing("Ownership Gate Unit")
    assert _transition(listing["id"], "verify", _admin_headers()).status_code == 200

    response = _transition(listing["id"], "activate", _other_landlord_headers())

    assert response.status_code == 403
    assert response.json()["detail"] == "owner or admin only"
    assert _status(listing["id"]) == "approved"

    assert _transition(listing["id"], "activate", _owner_headers()).status_code == 200
    assert _status(listing["id"]) == "active"


def test_student_cannot_activate_listing():
    listing = _submit_listing("Student Gate Unit")
    assert _transition(listing["id"], "verify", _admin_headers()).status_code == 200

    response = _transition(listing["id"], "activate", _student_headers())

    assert response.status_code == 403
    assert response.json()["detail"] == "owner or admin only"
    assert _status(listing["id"]) == "approved"


def test_admin_can_activate_listing_owned_by_another_user():
    listing = _submit_listing("Admin Override Unit")
    assert _transition(listing["id"], "verify", _admin_headers()).status_code == 200

    response = _transition(listing["id"], "activate", _admin_headers())

    assert response.status_code == 200
    assert response.json()["listing"]["status"] == "active"
    assert _status(listing["id"]) == "active"


def test_other_landlord_cannot_deactivate_active_listing():
    listing = _submit_listing("Deactivate Gate Unit")
    assert _transition(listing["id"], "verify", _admin_headers()).status_code == 200
    assert _transition(listing["id"], "activate", _owner_headers()).status_code == 200

    response = _transition(listing["id"], "deactivate", _other_landlord_headers())

    assert response.status_code == 403
    assert response.json()["detail"] == "owner or admin only"
    assert _status(listing["id"]) == "active"

    assert _transition(listing["id"], "deactivate", _owner_headers()).status_code == 200
    assert _status(listing["id"]) == "inactive"


def test_other_landlord_cannot_resubmit_rejected_listing():
    listing = _submit_listing("Resubmit Gate Unit")
    assert _transition(listing["id"], "verify", _admin_headers()).status_code == 200
    assert _transition(listing["id"], "reject", _admin_headers()).status_code == 200
    assert _status(listing["id"]) == "rejected"

    response = _transition(listing["id"], "resubmit", _other_landlord_headers())

    assert response.status_code == 403
    assert response.json()["detail"] == "owner or admin only"
    assert _status(listing["id"]) == "rejected"

    assert _transition(listing["id"], "resubmit", _owner_headers()).status_code == 200
    assert _status(listing["id"]) == "pending"


def test_transition_to_unknown_listing_returns_404():
    response = _transition(999999, "verify", _admin_headers())

    assert response.status_code == 404
    assert response.json()["detail"] == "listing not found"


def test_unknown_transition_action_returns_400():
    listing = _submit_listing("Invalid Action Unit")

    response = _transition(listing["id"], "destroy", _owner_headers())

    assert response.status_code == 400
    assert "action must be one of" in response.json()["detail"]
    assert _status(listing["id"]) == "pending"


def test_illegal_transition_by_owner_returns_400_without_state_change():
    listing = _submit_listing("Illegal Transition Unit")

    response = _transition(listing["id"], "activate", _owner_headers())

    assert response.status_code == 400
    assert "invalid transition" in response.json()["detail"]
    assert _status(listing["id"]) == "pending"
