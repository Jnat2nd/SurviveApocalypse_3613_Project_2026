"""Unit tests for auth utilities, lifecycle, and transition authorization."""

import asyncio
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import get_settings
from app.dependencies.auth import get_api_user
from app.models.listing import Listing
from app.models.user import User
from app.services import listings
from app.services.auth_service import AuthService
from app.services.lifecycle import (
    ACTIONS,
    apply_action,
    fields_for_status,
    messageable,
    normalize_status,
    snapshot,
    state_for,
    visible_to_students,
)
from app.utilities.security import (
    create_access_token,
    encrypt_password,
    verify_password,
)


def test_password_hash_round_trip_and_rejects_wrong_password():
    password_hash = encrypt_password("correct horse")

    assert password_hash != "correct horse"
    assert verify_password("correct horse", password_hash)
    assert not verify_password("wrong horse", password_hash)


def test_authenticate_user_returns_token_for_valid_credentials(monkeypatch):
    user = SimpleNamespace(
        id=7,
        username="alice",
        password=encrypt_password("secret"),
        role="regular_user",
    )

    class FakeUserRepository:
        def get_by_username(self, username):
            return user if username == "alice" else None

    monkeypatch.setattr(
        "app.services.auth_service.create_access_token",
        lambda data: {"sub": data["sub"], "role": data["role"]},
    )

    token = AuthService(FakeUserRepository()).authenticate_user("alice", "secret")

    assert token == {"sub": "7", "role": "regular_user"}


def test_authenticate_user_returns_none_for_invalid_credentials():
    user = SimpleNamespace(
        id=7,
        username="alice",
        password=encrypt_password("secret"),
        role="regular_user",
    )

    class FakeUserRepository:
        def get_by_username(self, username):
            return user if username == "alice" else None

    service = AuthService(FakeUserRepository())

    assert service.authenticate_user("alice", "incorrect") is None
    assert service.authenticate_user("missing", "secret") is None


def test_access_token_contains_subject_and_role():
    token = create_access_token({"sub": "7", "role": "admin"})
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[settings.jwt_algorithm],
    )

    assert payload["sub"] == "7"
    assert payload["role"] == "admin"
    assert "exp" in payload


FLAG_KEYS = (
    "flag_pending",
    "flag_verified",
    "flag_active",
    "flag_rejected",
)


def _row(status, **overrides):
    row = {
        "id": 1,
        "status": status,
        "flag_pending": 0,
        "flag_verified": 0,
        "flag_active": 0,
        "flag_rejected": 0,
    }
    row.update(overrides)
    return row


def _assert_result(result, status, flags):
    assert result["ok"] is True
    assert result["row"]["status"] == status
    assert result["listing"]["status"] == status
    assert tuple(result["row"][key] for key in FLAG_KEYS) == flags
    assert tuple(result["listing"][key] for key in FLAG_KEYS) == tuple(
        bool(flag) for flag in flags
    )


def test_pending_to_approved_via_verify():
    result = apply_action(_row("pending"), "verify")

    _assert_result(result, "approved", (0, 1, 0, 0))
    assert result["changed"] is True


def test_pending_to_approved_via_approve():
    assert "approve" in ACTIONS

    result = apply_action(_row("pending"), "approve")

    _assert_result(result, "approved", (0, 1, 0, 0))
    assert result["changed"] is True


def test_pending_to_rejected():
    result = apply_action(_row("pending"), "reject")

    _assert_result(result, "rejected", (0, 0, 0, 1))
    assert result["changed"] is True


def test_approved_to_active():
    result = apply_action(_row("approved"), "activate")

    _assert_result(result, "active", (0, 0, 1, 0))
    assert result["changed"] is True


def test_active_to_inactive():
    result = apply_action(_row("active"), "deactivate")

    _assert_result(result, "inactive", (0, 0, 0, 0))
    assert result["changed"] is True


def test_rejected_to_pending_resubmit():
    result = apply_action(_row("rejected"), "resubmit")

    _assert_result(result, "pending", (1, 0, 0, 0))
    assert result["changed"] is True


def test_submit_is_noop_only_from_pending():
    noop = apply_action(_row("pending"), "submit")

    _assert_result(noop, "pending", (1, 0, 0, 0))
    assert noop["changed"] is False

    invalid = apply_action(_row("active"), "submit")

    assert invalid["ok"] is False
    assert invalid["code"] == "invalid_transition"
    assert "invalid transition" in invalid["error"]


@pytest.mark.parametrize(
    ("status", "action"),
    [
        ("pending", "activate"),
        ("pending", "deactivate"),
        ("pending", "resubmit"),
        ("approved", "verify"),
        ("approved", "approve"),
        ("approved", "deactivate"),
        ("approved", "resubmit"),
        ("rejected", "verify"),
        ("rejected", "approve"),
        ("rejected", "activate"),
        ("rejected", "deactivate"),
        ("rejected", "reject"),
        ("rejected", "submit"),
        ("active", "verify"),
        ("active", "approve"),
        ("active", "activate"),
        ("active", "reject"),
        ("active", "resubmit"),
        ("active", "submit"),
        ("inactive", "verify"),
        ("inactive", "approve"),
        ("inactive", "deactivate"),
        ("inactive", "reject"),
        ("inactive", "resubmit"),
        ("inactive", "submit"),
    ],
)
def test_invalid_transitions(status, action):
    result = apply_action(_row(status), action)

    assert result["ok"] is False
    assert result["code"] == "invalid_transition"
    assert "invalid transition" in result["error"]


def test_unknown_action_returns_error():
    result = apply_action(_row("pending"), "destroy")

    assert result["ok"] is False
    assert result["code"] == "invalid_action"
    assert "unknown action" in result["error"]


def test_unknown_status_returns_invalid_state_code():
    result = apply_action(_row("mystery"), "verify")

    assert result["ok"] is False
    assert result["code"] == "invalid_state"
    assert "unknown listing status" in result["error"]


def test_verified_alias_maps_to_approved():
    assert normalize_status("verified") == "approved"
    assert state_for("verified").value == "approved"
    assert fields_for_status("verified") == {
        "status": "approved",
        "flag_pending": 0,
        "flag_verified": 1,
        "flag_active": 0,
        "flag_rejected": 0,
    }

    result = apply_action(_row("verified"), "activate")

    _assert_result(result, "active", (0, 0, 1, 0))


@pytest.mark.parametrize(
    ("status", "flags"),
    [
        ("pending", (True, False, False, False)),
        ("approved", (False, True, False, False)),
        ("verified", (False, True, False, False)),
        ("rejected", (False, False, False, True)),
        ("active", (False, False, True, False)),
        ("inactive", (False, False, False, False)),
    ],
)
def test_flags_are_derived_from_status(status, flags):
    listed = _row(
        status,
        flag_pending=1,
        flag_verified=1,
        flag_active=1,
        flag_rejected=1,
    )

    result = snapshot(listed)

    assert result["status"] == normalize_status(status)
    assert tuple(result[key] for key in FLAG_KEYS) == flags


def test_stale_active_flag_does_not_make_pending_visible():
    listed = _row("pending", flag_active=1)

    assert visible_to_students(listed) is False
    assert messageable(listed) is False


def test_reject_clears_stale_active_flag():
    result = apply_action(_row("pending", flag_active=1), "reject")

    _assert_result(result, "rejected", (0, 0, 0, 1))


def test_resubmit_clears_rejected_flag():
    listed = _row(
        "rejected",
        flag_rejected=1,
        flag_verified=1,
        flag_active=1,
    )

    result = apply_action(listed, "resubmit")

    _assert_result(result, "pending", (1, 0, 0, 0))


def _request(headers):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/student/listings",
        "raw_path": b"/api/student/listings",
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "headers": [
            (key.lower().encode(), value.encode()) for key, value in headers.items()
        ],
    }
    return Request(scope, receive)


def _patch_auth_settings(monkeypatch, env):
    settings = get_settings()
    monkeypatch.setattr(
        "app.dependencies.auth.get_settings",
        lambda: SimpleNamespace(
            secret_key=settings.secret_key,
            jwt_algorithm=settings.jwt_algorithm,
            env=env,
        ),
    )


def _make_user(session, username, role):
    user = User(
        username=username,
        email=f"{username}@example.com",
        password="not-used",
        role=role,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def test_get_api_user_resolves_identity_from_valid_bearer_token(test_session, monkeypatch):
    user = _make_user(test_session, "bearer-student", "student")
    _patch_auth_settings(monkeypatch, "production")
    token = create_access_token({"sub": str(user.id), "role": user.role})

    resolved = asyncio.run(
        get_api_user(_request({"Authorization": f"Bearer {token}"}), test_session)
    )

    assert resolved.id == user.id
    assert resolved.role == "student"


@pytest.mark.parametrize(
    "token",
    [
        "not-a-jwt",
        "",
        "eyJhbGciOiJIUzI1NiJ9.invalid.signature",
    ],
)
def test_get_api_user_rejects_malformed_bearer_token(test_session, monkeypatch, token):
    _patch_auth_settings(monkeypatch, "production")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            get_api_user(_request({"Authorization": f"Bearer {token}"}), test_session)
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


def test_get_api_user_rejects_token_with_forged_signature(test_session, monkeypatch):
    _patch_auth_settings(monkeypatch, "production")
    token = jwt.encode(
        {"sub": "1"}, "a-completely-different-secret-key-123", algorithm="HS256"
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            get_api_user(_request({"Authorization": f"Bearer {token}"}), test_session)
        )

    assert exc_info.value.status_code == 401


def test_get_api_user_rejects_non_integer_subject(test_session, monkeypatch):
    _patch_auth_settings(monkeypatch, "production")
    token = create_access_token({"sub": "not-an-int", "role": "student"})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            get_api_user(_request({"Authorization": f"Bearer {token}"}), test_session)
        )

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Could not validate credentials"


def test_get_api_user_rejects_subject_with_no_matching_user(test_session, monkeypatch):
    _patch_auth_settings(monkeypatch, "production")
    token = create_access_token({"sub": "987654", "role": "student"})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            get_api_user(_request({"Authorization": f"Bearer {token}"}), test_session)
        )

    assert exc_info.value.status_code == 401


def test_get_api_user_rejects_empty_bearer_value(test_session, monkeypatch):
    _patch_auth_settings(monkeypatch, "production")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            get_api_user(_request({"Authorization": "Bearer "}), test_session)
        )

    assert exc_info.value.status_code == 401


def test_get_api_user_accepts_legacy_header_only_in_development(test_session, monkeypatch):
    user = _make_user(test_session, "legacy-student", "student")
    headers = {"X-User-Id": str(user.id)}

    _patch_auth_settings(monkeypatch, "development")
    resolved = asyncio.run(get_api_user(_request(headers), test_session))
    assert resolved.id == user.id

    _patch_auth_settings(monkeypatch, "production")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_api_user(_request(headers), test_session))
    assert exc_info.value.status_code == 401


def test_get_api_user_rejects_non_integer_legacy_header(test_session, monkeypatch):
    _patch_auth_settings(monkeypatch, "development")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_api_user(_request({"X-User-Id": "abc"}), test_session))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "X-User-Id must be int"


def test_get_api_user_rejects_unknown_legacy_user(test_session, monkeypatch):
    _patch_auth_settings(monkeypatch, "development")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(get_api_user(_request({"X-User-Id": "987654"}), test_session))

    assert exc_info.value.status_code == 401
    assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}


ADMIN = {"id": 1, "role": "admin"}
OWNER = {"id": 2, "role": "landlord"}
OTHER_LANDLORD = {"id": 3, "role": "landlord"}
STUDENT = {"id": 4, "role": "student"}


class _FakeSession:
    def __init__(self, listing):
        self.listing = listing
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, model, listing_id):
        return self.listing if self.listing is not None and listing_id == self.listing.id else None

    def add(self, obj):
        pass

    def commit(self):
        self.commits += 1

    def refresh(self, obj):
        pass


def _listing(status="pending", landlord_id=OWNER["id"]):
    return Listing(
        id=10,
        landlord_id=landlord_id,
        title="Ownership Unit",
        area="st_augustine",
        price=1500,
        amenities="wifi",
        contact_phone="555-0100",
        contact_email="owner@example.com",
        photos="",
        status=status,
        flag_pending=int(status == "pending"),
        flag_verified=int(status == "approved"),
        flag_active=int(status == "active"),
        flag_rejected=int(status == "rejected"),
        created_at="2099-01-01T10:00:00+00:00",
    )


def _patch_dependencies(monkeypatch, actor, listing):
    session = _FakeSession(listing)
    monkeypatch.setattr(
        listings, "get_user", lambda user_id: actor if user_id == actor["id"] else None
    )
    monkeypatch.setattr(listings, "Session", lambda engine: session)
    return session


def test_unknown_actor_is_forbidden(monkeypatch):
    listing = _listing()
    session = _patch_dependencies(monkeypatch, ADMIN, listing)
    monkeypatch.setattr(listings, "get_user", lambda user_id: None)

    result = listings.transition(ADMIN["id"], listing.id, "verify")

    assert result == {
        "ok": False,
        "code": "forbidden",
        "error": "unknown user",
    }
    assert session.commits == 0


def test_unknown_action_is_rejected_before_state_change(monkeypatch):
    listing = _listing()
    session = _patch_dependencies(monkeypatch, ADMIN, listing)

    result = listings.transition(ADMIN["id"], listing.id, "destroy")

    assert result["ok"] is False
    assert result["code"] == "invalid_action"
    assert "action must be one of" in result["error"]
    assert session.commits == 0
    assert listing.status == "pending"


def test_missing_listing_returns_not_found(monkeypatch):
    session = _patch_dependencies(monkeypatch, ADMIN, None)

    result = listings.transition(ADMIN["id"], 10, "verify")

    assert result == {
        "ok": False,
        "code": "not_found",
        "error": "listing not found",
    }
    assert session.commits == 0


@pytest.mark.parametrize("action", ["verify", "approve", "reject"])
@pytest.mark.parametrize("actor", [OWNER, OTHER_LANDLORD, STUDENT])
def test_admin_only_actions_forbid_non_admins(monkeypatch, action, actor):
    listing = _listing()
    session = _patch_dependencies(monkeypatch, actor, listing)

    result = listings.transition(actor["id"], listing.id, action)

    assert result["ok"] is False
    assert result["code"] == "forbidden"
    assert result["error"] == "admin only"
    assert listing.status == "pending"
    assert session.commits == 0


@pytest.mark.parametrize("action", ["activate", "deactivate", "resubmit", "submit"])
def test_owner_actions_forbid_non_owner_landlord(monkeypatch, action):
    listing = _listing(status="approved")
    session = _patch_dependencies(monkeypatch, OTHER_LANDLORD, listing)

    result = listings.transition(OTHER_LANDLORD["id"], listing.id, action)

    assert result["ok"] is False
    assert result["code"] == "forbidden"
    assert result["error"] == "owner or admin only"
    assert listing.status == "approved"
    assert session.commits == 0


@pytest.mark.parametrize("action", ["activate", "deactivate", "resubmit", "submit"])
def test_owner_actions_forbid_student(monkeypatch, action):
    listing = _listing(status="approved")
    session = _patch_dependencies(monkeypatch, STUDENT, listing)

    result = listings.transition(STUDENT["id"], listing.id, action)

    assert result["ok"] is False
    assert result["code"] == "forbidden"
    assert result["error"] == "owner or admin only"
    assert session.commits == 0


def test_owner_can_activate_own_listing(monkeypatch):
    listing = _listing(status="approved")
    session = _patch_dependencies(monkeypatch, OWNER, listing)

    result = listings.transition(OWNER["id"], listing.id, "activate")

    assert result["ok"] is True
    assert result["listing"]["status"] == "active"
    assert result["listing"]["flag_active"] == 1
    assert listing.status == "active"
    assert session.commits == 1


def test_admin_can_activate_listing_owned_by_someone_else(monkeypatch):
    listing = _listing(status="approved")
    session = _patch_dependencies(monkeypatch, ADMIN, listing)

    result = listings.transition(ADMIN["id"], listing.id, "activate")

    assert result["ok"] is True
    assert listing.status == "active"
    assert session.commits == 1


@pytest.mark.parametrize("action", ["verify", "approve", "reject"])
def test_admin_can_run_admin_only_actions(monkeypatch, action):
    listing = _listing()
    session = _patch_dependencies(monkeypatch, ADMIN, listing)

    result = listings.transition(ADMIN["id"], listing.id, action)

    assert result["ok"] is True
    assert session.commits == 1


def test_owner_activation_from_pending_is_invalid_transition(monkeypatch):
    listing = _listing(status="pending")
    session = _patch_dependencies(monkeypatch, OWNER, listing)

    result = listings.transition(OWNER["id"], listing.id, "activate")

    assert result["ok"] is False
    assert result["code"] == "invalid_transition"
    assert "invalid transition" in result["error"]
    assert listing.status == "pending"
    assert session.commits == 0


def test_unknown_status_reports_invalid_state_code(monkeypatch):
    listing = _listing(status="mystery")
    session = _patch_dependencies(monkeypatch, ADMIN, listing)

    result = listings.transition(ADMIN["id"], listing.id, "verify")

    assert result["ok"] is False
    assert result["code"] == "invalid_state"
    assert "unknown listing status" in result["error"]
    assert session.commits == 0
