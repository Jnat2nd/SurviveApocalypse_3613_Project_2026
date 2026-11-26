"""Integration tests: API contracts, authentication, persistence, and seeding."""

import jwt
import pytest
from sqlmodel import select

from app.config import get_settings
from app.models.listing import Listing
from app.models.message import Message
from app.models.user import User
from app.repositories.user import UserRepository
from app.schemas.user import RegularUserCreate, UserUpdate
from app.seed import seed
from app.services.auth_service import AuthService
from app.services.user_service import UserService
from app.utilities.security import verify_password


def _detail(response):
    return response.json()["detail"]


class TestHealthAndPublicApi:
    def test_health_returns_scenario_metadata(self, client):
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "status": "ok",
            "scenario": "student-accommodation",
            "version": "0.2.0",
        }

    @pytest.mark.parametrize("path", ["/", "/login", "/register", "/docs"])
    def test_public_pages_render(self, client, path):
        assert client.get(path).status_code == 200

    def test_openapi_documents_scenario_routes(self, client):
        response = client.get("/openapi.json")

        assert response.status_code == 200
        paths = response.json()["paths"]
        assert "/api/auth/login" in paths
        assert "/api/student/listings" in paths
        assert "/api/admin/listings" in paths
        assert "/api/landlord/listings" in paths
        assert "/api/listings/{listing_id}/transition" in paths
        assert "/api/student/messages" in paths

    def test_config_panel_is_disabled_by_default(self, client):
        assert client.get("/config/login").status_code == 404
        assert client.get("/config").status_code == 404


class TestAuthenticationEndpoints:
    def test_login_admin_returns_public_user(self, client):
        response = client.post(
            "/api/auth/login",
            json={"email": "admin@example.com", "password": "admin-dev"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["user"]["role"] == "admin"
        assert "password" not in body["user"]
        assert body["token_type"] == "bearer"

    def test_login_returns_verifiable_bearer_token(self, client):
        response = client.post(
            "/api/auth/login",
            json={"email": "student@example.com", "password": "stud-dev"},
        )

        assert response.status_code == 200
        body = response.json()
        settings = get_settings()
        payload = jwt.decode(
            body["access_token"],
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
        )
        assert payload["sub"] == str(body["user"]["id"])
        assert payload["role"] == body["user"]["role"]
        assert "exp" in payload

    def test_login_rejects_bad_credentials_with_401_json(self, client):
        response = client.post(
            "/api/auth/login",
            json={"email": "student@example.com", "password": "wrong"},
        )

        assert response.status_code == 401
        assert response.json() == {"detail": "invalid credentials"}

    def test_login_rejects_unknown_email(self, client):
        response = client.post(
            "/api/auth/login",
            json={"email": "nobody@example.com", "password": "stud-dev"},
        )

        assert response.status_code == 401
        assert response.json() == {"detail": "invalid credentials"}

    def test_login_requires_email_and_password(self, client):
        response = client.post("/api/auth/login", json={"email": "student@example.com"})

        assert response.status_code == 422
        assert any(error["loc"] == ["body", "password"] for error in response.json()["detail"])

    def test_api_without_credentials_returns_401_json_with_bearer_challenge(self, client):
        response = client.get("/api/student/listings")

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json() == {"detail": "Could not validate credentials"}

    def test_api_with_invalid_bearer_token_returns_401_json(self, client):
        response = client.get(
            "/api/student/listings", headers={"Authorization": "Bearer invalid-token"}
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "Could not validate credentials"

    def test_bearer_token_authenticates_protected_api_routes(self, api):
        assert (
            api.client.get(
                "/api/student/listings", headers=api.headers_for("student")
            ).status_code
            == 200
        )
        assert (
            api.client.get("/api/users", headers=api.headers_for("admin")).status_code
        ) == 200
        assert (
            api.client.get(
                "/api/admin/listings", headers=api.headers_for("admin")
            ).status_code
        ) == 200

        submitted = api.client.post(
            "/api/landlord/listings",
            json={
                "title": "Bearer Auth Unit",
                "area": "st_augustine",
                "price": 1200,
                "amenities": "wifi",
                "contact_phone": "555-0123",
                "contact_email": "landlord@example.com",
            },
            headers=api.headers_for("landlord"),
        )
        assert submitted.status_code == 200
        assert submitted.json()["listing"]["landlord_id"] is not None

    @pytest.mark.parametrize("role", ["student", "landlord"])
    def test_non_admin_bearer_token_cannot_use_admin_routes(self, api, role):
        headers = api.headers_for(role)

        for path in ("/api/users", "/api/admin/listings"):
            response = api.client.get(path, headers=headers)
            assert response.status_code == 403
            assert _detail(response) == "admin only"

    def test_non_api_401_still_renders_sign_in_page(self, client):
        response = client.get("/app")

        assert response.status_code == 401
        assert "Sign in required" in response.text
        assert "Return to sign in" in response.text

    @pytest.mark.skipif(
        get_settings().env.lower() not in {"dev", "development"},
        reason="legacy X-User-Id fallback is development-only",
    )
    def test_legacy_header_still_authenticates_end_to_end_in_development(self, api):
        user_id = api.user_id("student@example.com", "stud-dev")

        response = api.client.get(
            "/api/student/listings", headers={"X-User-Id": str(user_id)}
        )

        assert response.status_code == 200
        assert "listings" in response.json()

    def test_legacy_header_is_rejected_in_production(self, api, monkeypatch):
        from types import SimpleNamespace

        settings = get_settings()
        monkeypatch.setattr(
            "app.dependencies.auth.get_settings",
            lambda: SimpleNamespace(
                secret_key=settings.secret_key,
                jwt_algorithm=settings.jwt_algorithm,
                env="production",
            ),
        )
        user_id = api.user_id("student@example.com", "stud-dev")

        response = api.client.get(
            "/api/student/listings", headers={"X-User-Id": str(user_id)}
        )

        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


class TestListingEndpoints:
    def test_student_browse_returns_only_visible_listings(self, api):
        response = api.client.get(
            "/api/student/listings", headers=api.headers_for("student")
        )

        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        titles = [listing["title"] for listing in body["listings"]]
        assert "Verified Active Flat" in titles
        assert "Pending Unit" not in titles
        assert all(listing["status"] == "active" for listing in body["listings"])

    def test_admin_list_returns_all_listings_without_ok_key(self, api):
        response = api.client.get(
            "/api/admin/listings", headers=api.headers_for("admin")
        )

        assert response.status_code == 200
        body = response.json()
        assert "ok" not in body
        assert {item["status"] for item in body["listings"]} >= {"active", "pending"}

    def test_users_endpoint_excludes_password_and_role(self, api):
        response = api.client.get("/api/users", headers=api.headers_for("admin"))

        assert response.status_code == 200
        users = response.json()
        assert len(users) == 5
        assert all(set(user) == {"id", "username", "email"} for user in users)

    def test_student_cannot_message_pending_listing(self, api):
        pending = api.admin_listing("Pending Unit")

        response = api.client.post(
            "/api/student/messages",
            json={"listing_id": pending["id"], "body": "Is this still available?"},
            headers=api.headers_for("student"),
        )

        assert response.status_code == 400
        assert "not available for messaging" in _detail(response)

    @pytest.mark.parametrize(
        ("case", "expected_code", "expected_detail"),
        [
            ("forbidden", 403, "admin only"),
            ("not_found", 404, "listing not found"),
            ("invalid_action", 400, "action must be one of"),
            ("invalid_transition", 400, "invalid transition"),
        ],
    )
    def test_transition_error_status_mapping(self, api, case, expected_code, expected_detail):
        listing = api.submit(f"Error Map {case}")

        if case == "forbidden":
            response = api.transition(listing["id"], "verify", api.headers_for("landlord"))
        elif case == "not_found":
            response = api.transition(999999, "verify", api.headers_for("admin"))
        elif case == "invalid_action":
            response = api.transition(listing["id"], "destroy", api.headers_for("landlord"))
        else:
            response = api.transition(listing["id"], "activate", api.headers_for("landlord"))

        assert response.status_code == expected_code
        assert expected_detail in _detail(response)
        assert api.status(listing["id"]) == "pending"

    @pytest.mark.parametrize(
        ("method", "path", "kwargs"),
        [
            (
                "post",
                "/api/landlord/listings",
                {"json": {"title": "Bad", "area": "x", "price": "many", "contact_phone": "1", "contact_email": "x@y.z"}},
            ),
            ("post", "/api/listings/not-an-int/transition", {"json": {"action": "verify"}}),
            ("post", "/api/listings/1/transition", {"json": {}}),
            ("post", "/api/student/messages", {"json": {"body": "hello"}}),
            ("get", "/api/student/listings", {"params": {"max_price": "expensive"}}),
        ],
    )
    def test_request_validation_returns_422(self, client, api, method, path, kwargs):
        if path.startswith("/api/listings"):
            headers = api.headers_for("admin")
        elif path in {"/api/student/listings", "/api/student/messages"}:
            headers = api.headers_for("student")
        elif path == "/api/landlord/listings":
            headers = api.headers_for("landlord")
        else:
            headers = {}

        response = getattr(client, method)(path, headers=headers, **kwargs)

        assert response.status_code == 422
        assert isinstance(response.json()["detail"], list)


class TestPersistenceThroughApi:
    def test_submitted_listing_is_stored_as_pending(self, api, persistence_session):
        listing = api.submit("Persisted Pending Unit")

        stored = persistence_session.get(Listing, listing["id"])

        assert stored is not None
        assert stored.status == "pending"
        assert (stored.flag_pending, stored.flag_verified, stored.flag_active, stored.flag_rejected) == (1, 0, 0, 0)
        assert stored.title == "Persisted Pending Unit"

    def test_transition_updates_persisted_status_and_flags(self, api, persistence_session):
        listing = api.submit("Persisted Transition Unit")
        assert api.transition(listing["id"], "verify", api.headers_for("admin")).status_code == 200
        assert api.transition(listing["id"], "activate", api.headers_for("admin")).status_code == 200

        stored = persistence_session.get(Listing, listing["id"])

        assert stored.status == "active"
        assert (stored.flag_pending, stored.flag_verified, stored.flag_active, stored.flag_rejected) == (0, 0, 1, 0)

    def test_message_endpoint_persists_message(self, api, persistence_session):
        active = next(
            item for item in api.browse("student") if item["title"] == "Verified Active Flat"
        )

        response = api.client.post(
            "/api/student/messages",
            json={"listing_id": active["id"], "body": "Please contact me"},
            headers=api.headers_for("student"),
        )

        assert response.status_code == 200
        message_id = response.json()["message_id"]
        stored = persistence_session.get(Message, message_id)
        assert stored is not None
        assert stored.listing_id == active["id"]
        assert stored.student_id == api.user_id("student@example.com", "stud-dev")
        assert stored.body == "Please contact me"

    def test_legacy_verified_status_row_is_visible_to_admin_filter(self, api, persistence_session):
        listing = api.submit("Legacy Status Row")
        stored = persistence_session.get(Listing, listing["id"])
        stored.status = "verified"
        persistence_session.add(stored)
        persistence_session.commit()

        response = api.client.get(
            "/api/admin/listings",
            params={"status": "approved"},
            headers=api.headers_for("admin"),
        )

        assert response.status_code == 200
        assert any(item["id"] == listing["id"] for item in response.json()["listings"])


class TestDatabaseServices:
    def test_save_password(self, test_session):
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

    def test_login_service_returns_token_for_registered_user(self, test_session):
        repository = UserRepository(test_session)
        service = AuthService(repository)
        service.register_user("alice", "alice@example.com", "secret")

        token = service.authenticate_user("alice", "secret")
        settings = get_settings()
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm]
        )

        assert payload["sub"] == str(repository.get_by_username("alice").id)
        assert payload["role"] == "regular_user"

    def test_register_user_is_visible_through_user_service(self, test_session):
        repository = UserRepository(test_session)
        AuthService(repository).register_user("alice", "alice@example.com", "secret")

        users = UserService(repository).get_all_users()

        assert len(users) == 1
        assert users[0].username == "alice"
        assert users[0].email == "alice@example.com"

    def test_user_repository_crud_and_search(self, test_session):
        repository = UserRepository(test_session)
        user = repository.create(
            RegularUserCreate(username="bob", email="bob@example.com", password="hashed", role="student")
        )

        assert repository.get_by_id(user.id).username == "bob"
        found, pagination = repository.search_users("bob")
        assert [item.id for item in found] == [user.id]
        assert pagination.total_count == 1

        updated = repository.update_user(
            user.id, UserUpdate(username="bobby", email="bobby@example.com")
        )
        assert updated.username == "bobby"
        assert updated.email == "bobby@example.com"

        repository.delete_user(user.id)
        assert repository.get_by_id(user.id) is None

    @pytest.mark.parametrize(
        ("method", "identifier"), [("update_user", 999), ("delete_user", 999)]
    )
    def test_user_repository_rejects_unknown_ids(self, test_session, method, identifier):
        repository = UserRepository(test_session)
        expected = "Invalid user id given" if method == "update_user" else "User doesn't exist"

        with pytest.raises(Exception, match=expected):
            if method == "update_user":
                repository.update_user(
                    identifier, UserUpdate(username="x", email="x@example.com")
                )
            else:
                repository.delete_user(identifier)

    def test_seed_is_idempotent_and_resettable(self, seeded_session):
        seed(reset=False)
        seeded_session.expire_all()

        assert len(seeded_session.exec(select(User)).all()) == 5
        assert len(seeded_session.exec(select(Listing)).all()) == 2

        seed(reset=True)
        seeded_session.expire_all()

        assert len(seeded_session.exec(select(User)).all()) == 5
        assert len(seeded_session.exec(select(Listing)).all()) == 2
