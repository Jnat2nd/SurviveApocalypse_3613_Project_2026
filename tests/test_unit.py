"""Unit tests: pure functions, service logic, helpers, and isolated dependencies."""

import asyncio
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError, ProgrammingError
from starlette.requests import Request

from app.config import (
    get_settings,
    mask_database_uri,
    mask_secret,
    set_runtime_env,
)
from app.database import is_db_not_ready_error, sqlalchemy_uri
from app.dependencies.auth import get_api_user, is_admin, is_admin_dep, is_api_admin_dep
from app.models.listing import Listing
from app.models.message import Message
from app.models.user import User
from app.services import listings
from app.services.auth_service import AuthService
from app.services.housing_auth import is_eligible_student, public_user
from app.services.lifecycle import (
    ACTIONS,
    InvalidTransitionError,
    apply_action,
    fields_for_status,
    messageable,
    normalize_status,
    snapshot,
    state_for,
    visible_to_students,
)
from app.services.user_service import UserService
from app.utilities.flash import flash, get_flashed_messages
from app.utilities.pagination import Pagination
from app.utilities.security import (
    access_token_cookie_kwargs,
    create_access_token,
    encrypt_password,
    verify_password,
)


class TestSecurityUtilities:
    def test_password_hash_round_trip_and_rejects_wrong_password(self):
        password_hash = encrypt_password("correct horse")

        assert password_hash != "correct horse"
        assert verify_password("correct horse", password_hash)
        assert not verify_password("wrong horse", password_hash)

    def test_different_passwords_produce_different_hashes(self):
        assert encrypt_password("one") != encrypt_password("one")
        assert encrypt_password("one") != encrypt_password("two")

    def test_access_token_contains_subject_role_and_expiry(self):
        token = create_access_token({"sub": "7", "role": "admin"})
        settings = get_settings()
        payload = jwt.decode(
            token, settings.secret_key, algorithms=[settings.jwt_algorithm]
        )

        assert payload["sub"] == "7"
        assert payload["role"] == "admin"
        assert "exp" in payload

    def test_access_token_cookie_flags_follow_environment(self, monkeypatch):
        monkeypatch.setattr(
            "app.utilities.security.get_settings", lambda: SimpleNamespace(env="production")
        )
        production = access_token_cookie_kwargs()

        assert production == {
            "httponly": True,
            "samesite": "none",
            "secure": True,
        }

        monkeypatch.setattr(
            "app.utilities.security.get_settings", lambda: SimpleNamespace(env="development")
        )
        development = access_token_cookie_kwargs()

        assert development == {
            "httponly": True,
            "samesite": "lax",
            "secure": False,
        }


class FakeUserRepository:
    def __init__(self, user=None):
        self.user = user
        self.created = []

    def get_by_username(self, username):
        return self.user if username == "alice" else None

    def get_all_users(self):
        return [self.user] if self.user else []

    def create(self, user_data):
        self.created.append(user_data)
        return SimpleNamespace(id=99, **user_data.model_dump())


class TestAuthService:
    def test_authenticate_user_returns_token_for_valid_credentials(self, monkeypatch):
        user = SimpleNamespace(
            id=7,
            username="alice",
            password=encrypt_password("secret"),
            role="regular_user",
        )
        monkeypatch.setattr(
            "app.services.auth_service.create_access_token",
            lambda data: {"sub": data["sub"], "role": data["role"]},
        )

        token = AuthService(FakeUserRepository(user)).authenticate_user("alice", "secret")

        assert token == {"sub": "7", "role": "regular_user"}

    def test_authenticate_user_returns_none_for_invalid_credentials(self):
        user = SimpleNamespace(
            id=7,
            username="alice",
            password=encrypt_password("secret"),
            role="regular_user",
        )
        service = AuthService(FakeUserRepository(user))

        assert service.authenticate_user("alice", "incorrect") is None
        assert service.authenticate_user("missing", "secret") is None

    def test_register_user_hashes_password_and_creates_regular_user(self):
        repository = FakeUserRepository()
        created = AuthService(repository).register_user(
            "alice", "alice@example.com", "secret"
        )

        assert len(repository.created) == 1
        data = repository.created[0]
        assert data.username == "alice"
        assert data.email == "alice@example.com"
        assert data.role == "regular_user"
        assert data.password != "secret"
        assert verify_password("secret", data.password)
        assert created.id == 99

    def test_user_service_delegates_to_repository(self):
        user = SimpleNamespace(id=1, username="alice")
        repository = FakeUserRepository(user)

        assert UserService(repository).get_all_users() == [user]


FLAG_KEYS = ("flag_pending", "flag_verified", "flag_active", "flag_rejected")


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


class TestLifecycleTransitions:
    def test_pending_to_approved_via_verify(self):
        result = apply_action(_row("pending"), "verify")

        _assert_result(result, "approved", (0, 1, 0, 0))
        assert result["changed"] is True

    def test_pending_to_approved_via_approve(self):
        assert "approve" in ACTIONS

        result = apply_action(_row("pending"), "approve")

        _assert_result(result, "approved", (0, 1, 0, 0))
        assert result["changed"] is True

    def test_pending_to_rejected(self):
        result = apply_action(_row("pending"), "reject")

        _assert_result(result, "rejected", (0, 0, 0, 1))
        assert result["changed"] is True

    def test_approved_to_active(self):
        result = apply_action(_row("approved"), "activate")

        _assert_result(result, "active", (0, 0, 1, 0))
        assert result["changed"] is True

    def test_approved_can_be_rejected(self):
        result = apply_action(_row("approved"), "reject")

        _assert_result(result, "rejected", (0, 0, 0, 1))

    def test_active_to_inactive(self):
        result = apply_action(_row("active"), "deactivate")

        _assert_result(result, "inactive", (0, 0, 0, 0))
        assert result["changed"] is True

    def test_inactive_to_active(self):
        result = apply_action(_row("inactive"), "activate")

        _assert_result(result, "active", (0, 0, 1, 0))

    def test_rejected_to_pending_resubmit(self):
        result = apply_action(_row("rejected"), "resubmit")

        _assert_result(result, "pending", (1, 0, 0, 0))
        assert result["changed"] is True

    def test_submit_is_noop_only_from_pending(self):
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
    def test_invalid_transitions(self, status, action):
        result = apply_action(_row(status), action)

        assert result["ok"] is False
        assert result["code"] == "invalid_transition"
        assert "invalid transition" in result["error"]

    def test_state_objects_reject_unknown_actions(self):
        with pytest.raises(InvalidTransitionError):
            state_for("pending").transition("destroy")

    def test_unknown_action_returns_error(self):
        result = apply_action(_row("pending"), "destroy")

        assert result["ok"] is False
        assert result["code"] == "invalid_action"
        assert "unknown action" in result["error"]

    def test_unknown_status_returns_invalid_state_code(self):
        result = apply_action(_row("mystery"), "verify")

        assert result["ok"] is False
        assert result["code"] == "invalid_state"
        assert "unknown listing status" in result["error"]

    def test_verified_alias_maps_to_approved(self):
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
        ("raw", "expected"),
        [("  PENDING  ", "pending"), ("Verified", "approved"), (None, "")],
    )
    def test_normalize_status_strips_and_lowercases(self, raw, expected):
        assert normalize_status(raw) == expected

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
    def test_flags_are_derived_from_status(self, status, flags):
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

    def test_snapshot_requires_known_status(self):
        with pytest.raises(ValueError, match="unknown listing status"):
            snapshot({})

    @pytest.mark.parametrize(
        ("status", "visible", "message_allowed"),
        [
            ("pending", False, False),
            ("approved", False, False),
            ("rejected", False, False),
            ("active", True, True),
            ("inactive", False, False),
        ],
    )
    def test_visibility_and_messaging_are_status_only(
        self, status, visible, message_allowed
    ):
        listed = _row(status, flag_active=1)

        assert visible_to_students(listed) is visible
        assert messageable(listed) is message_allowed

    def test_reject_clears_stale_active_flag(self):
        result = apply_action(_row("pending", flag_active=1), "reject")

        _assert_result(result, "rejected", (0, 0, 0, 1))

    def test_resubmit_clears_rejected_flag(self):
        listed = _row(
            "rejected",
            flag_rejected=1,
            flag_verified=1,
            flag_active=1,
        )

        result = apply_action(listed, "resubmit")

        _assert_result(result, "pending", (1, 0, 0, 0))


class FakeResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def all(self):
        return self.rows

    def one_or_none(self):
        return self.rows[0] if self.rows else None


class FakeSession:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.added = []
        self.deleted = []
        self.commits = 0
        self._next_id = 100

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = self._next_id
            self._next_id += 1

    def delete(self, obj):
        self.deleted.append(obj)

    def get(self, model, ident):
        return next((row for row in self.rows if row.id == ident), None)

    def exec(self, statement):
        sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
        rows = list(self.rows)
        status_match = re.search(r"listing\.status = '([^']+)'", sql)
        if status_match:
            rows = [row for row in rows if row.status == status_match.group(1)]
        in_match = re.search(r"listing\.status IN \(([^)]*)\)", sql)
        if in_match:
            statuses = set(re.findall(r"'([^']+)'", in_match.group(1)))
            rows = [row for row in rows if row.status in statuses]
        return FakeResult(rows)

    def commit(self):
        self.commits += 1

    def refresh(self, obj):
        pass


ADMIN = {"id": 1, "role": "admin"}
OWNER = {"id": 2, "role": "landlord"}
OTHER_LANDLORD = {"id": 3, "role": "landlord"}
STUDENT = {
    "id": 4,
    "role": "student",
    "student_id": "202012345",
    "provisional": 0,
}


def _listing(status="pending", landlord_id=OWNER["id"], **overrides):
    fields = {
        "id": 10,
        "landlord_id": landlord_id,
        "title": "Ownership Unit",
        "area": "st_augustine",
        "price": 1500,
        "amenities": "wifi",
        "contact_phone": "555-0100",
        "contact_email": "owner@example.com",
        "photos": "",
        "status": status,
        "flag_pending": int(status == "pending"),
        "flag_verified": int(status == "approved"),
        "flag_active": int(status == "active"),
        "flag_rejected": int(status == "rejected"),
        "created_at": "2099-01-01T10:00:00+00:00",
    }
    fields.update(overrides)
    return Listing(**fields)


def _patch_listings(monkeypatch, actor, session):
    monkeypatch.setattr(
        listings, "get_user", lambda user_id: actor if user_id == (actor or {}).get("id") else None
    )
    monkeypatch.setattr(listings, "Session", lambda engine: session)
    return session


class TestSubmitListingService:
    def test_landlord_submits_pending_listing(self, monkeypatch):
        session = _patch_listings(monkeypatch, OWNER, FakeSession())

        result = listings.submit_listing(
            OWNER["id"],
            title="New Unit",
            area="mount_hope",
            price=1750,
            amenities="wifi,parking",
            contact_phone="555-0100",
            contact_email="owner@example.com",
        )

        assert result["ok"] is True
        assert result["listing"]["status"] == "pending"
        assert result["listing"]["flag_pending"] == 1
        assert result["listing"]["landlord_id"] == OWNER["id"]
        assert len(session.added) == 1
        assert session.added[0].title == "New Unit"
        assert session.commits == 1
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", result["listing"]["created_at"])

    @pytest.mark.parametrize("actor", [None, STUDENT, OWNER])
    def test_submit_requires_landlord_role(self, monkeypatch, actor):
        if actor is OWNER:
            actor = {"id": OWNER["id"], "role": "student"}
        session = _patch_listings(monkeypatch, actor, FakeSession())
        actor_id = OWNER["id"] if actor else 999

        result = listings.submit_listing(
            actor_id,
            title="Rejected Unit",
            area="mount_hope",
            price=1750,
            amenities="wifi",
            contact_phone="555-0100",
            contact_email="owner@example.com",
        )

        assert result == {"ok": False, "error": "landlords only"}
        assert session.added == []
        assert session.commits == 0

    @pytest.mark.parametrize("price", [0, -1])
    def test_submit_rejects_non_positive_price(self, monkeypatch, price):
        session = _patch_listings(monkeypatch, OWNER, FakeSession())

        result = listings.submit_listing(
            OWNER["id"],
            title="Bad Price",
            area="mount_hope",
            price=price,
            amenities="wifi",
            contact_phone="555-0100",
            contact_email="owner@example.com",
        )

        assert result == {"ok": False, "error": "price must be positive"}
        assert session.added == []
        assert session.commits == 0

    def test_submit_accepts_empty_amenities_and_any_area(self, monkeypatch):
        _patch_listings(monkeypatch, OWNER, FakeSession())

        result = listings.submit_listing(
            OWNER["id"],
            title="Unvalidated Unit",
            area="unknown-district",
            price=1,
            amenities="",
            contact_phone="555-0100",
            contact_email="owner@example.com",
        )

        assert result["ok"] is True
        assert result["listing"]["area"] == "unknown-district"
        assert result["listing"]["amenities"] == ""

    def test_parse_amenities_normalizes_and_ignores_blanks(self):
        assert listings._parse_amenities(" Wifi , Laundry , , ") == ["wifi", "laundry"]
        assert listings._parse_amenities("") == []
        assert listings._parse_amenities(None) == []

    def test_now_uses_utc_without_microseconds(self):
        value = listings._now()
        parsed = datetime.fromisoformat(value)

        assert parsed.tzinfo == timezone.utc
        assert parsed.microsecond == 0


class TestBrowseService:
    def test_only_active_listings_are_visible(self, monkeypatch):
        rows = [
            _listing(status="pending", id=1),
            _listing(status="approved", id=2),
            _listing(status="rejected", id=3),
            _listing(status="inactive", id=4),
            _listing(status="active", id=5, title="Visible"),
        ]
        _patch_listings(monkeypatch, STUDENT, FakeSession(rows))

        result = listings.browse_for_students(STUDENT["id"])

        assert result["ok"] is True
        assert [item["id"] for item in result["listings"]] == [5]
        assert result["listings"][0]["title"] == "Visible"

    @pytest.mark.parametrize(
        ("filters", "expected_ids"),
        [
            ({"area": "st_augustine"}, [11]),
            ({"max_price": 1500}, [11]),
            ({"amenity": "gym"}, [11]),
            ({"amenity": "GYM"}, [11]),
            ({"area": "st_augustine", "amenity": "gym"}, [11]),
        ],
    )
    def test_browse_filters(self, monkeypatch, filters, expected_ids):
        rows = [
            _listing(status="active", id=11, area="st_augustine", price=1500, amenities="wifi,gym"),
            _listing(status="active", id=12, area="mount_hope", price=4000, amenities="wifi,parking"),
        ]
        _patch_listings(monkeypatch, STUDENT, FakeSession(rows))

        result = listings.browse_for_students(STUDENT["id"], **filters)

        assert [item["id"] for item in result["listings"]] == expected_ids

    @pytest.mark.parametrize("actor", [None, ADMIN, OWNER])
    def test_browse_requires_student_role(self, monkeypatch, actor):
        _patch_listings(monkeypatch, actor, FakeSession([]))
        actor_id = (actor or {}).get("id", 999)

        result = listings.browse_for_students(actor_id)

        assert result == {"ok": False, "error": "students only"}

    def test_provisional_student_is_not_blocked(self, monkeypatch):
        provisional = {
            "id": 5,
            "role": "student",
            "student_id": None,
            "provisional": 1,
        }
        _patch_listings(monkeypatch, provisional, FakeSession([_listing(status="active")]))

        result = listings.browse_for_students(5)

        assert result["ok"] is True
        assert len(result["listings"]) == 1


class TestMessageService:
    def test_student_messages_active_listing(self, monkeypatch):
        session = _patch_listings(
            monkeypatch, STUDENT, FakeSession([_listing(status="active")])
        )

        result = listings.send_message(STUDENT["id"], 10, "  Is this still available?  ")

        assert result == {"ok": True, "message_id": 100}
        assert len(session.added) == 1
        assert isinstance(session.added[0], Message) is True
        assert session.added[0].body == "  Is this still available?  "
        assert session.added[0].listing_id == 10
        assert session.added[0].student_id == STUDENT["id"]
        assert session.commits == 1

    @pytest.mark.parametrize("actor", [None, ADMIN, OWNER])
    def test_messaging_requires_student_role(self, monkeypatch, actor):
        session = _patch_listings(monkeypatch, actor, FakeSession([_listing(status="active")]))

        result = listings.send_message((actor or {}).get("id", 999), 10, "hello")

        assert result == {"ok": False, "error": "students only"}
        assert session.added == []

    @pytest.mark.parametrize("body", ["", "   ", "\n\t"])
    def test_empty_message_body_is_rejected(self, monkeypatch, body):
        session = _patch_listings(
            monkeypatch, STUDENT, FakeSession([_listing(status="active")])
        )

        result = listings.send_message(STUDENT["id"], 10, body)

        assert result == {"ok": False, "error": "body required"}
        assert session.added == []

    def test_missing_listing_cannot_be_messaged(self, monkeypatch):
        session = _patch_listings(monkeypatch, STUDENT, FakeSession([]))

        result = listings.send_message(STUDENT["id"], 10, "hello")

        assert result == {"ok": False, "error": "listing not found"}
        assert session.added == []

    @pytest.mark.parametrize("status", ["pending", "approved", "rejected", "inactive"])
    def test_non_messageable_status_is_rejected(self, monkeypatch, status):
        session = _patch_listings(
            monkeypatch, STUDENT, FakeSession([_listing(status=status)])
        )

        result = listings.send_message(STUDENT["id"], 10, "hello")

        assert result == {"ok": False, "error": "listing not available for messaging"}
        assert session.added == []


class TestAdminListService:
    def test_list_all_returns_every_listing(self, monkeypatch):
        rows = [_listing(status="pending", id=1), _listing(status="active", id=2)]
        _patch_listings(monkeypatch, ADMIN, FakeSession(rows))

        result = listings.list_all()

        assert [item["id"] for item in result] == [1, 2]

    @pytest.mark.parametrize("requested", ["approved", "verified"])
    def test_list_all_includes_legacy_verified_status(self, monkeypatch, requested):
        rows = [
            _listing(status="pending", id=1),
            _listing(status="approved", id=2),
            _listing(status="verified", id=3),
            _listing(status="rejected", id=4),
        ]
        _patch_listings(monkeypatch, ADMIN, FakeSession(rows))

        result = listings.list_all(status=requested)

        assert {item["id"] for item in result} == {2, 3}

    def test_list_all_filters_other_statuses(self, monkeypatch):
        rows = [
            _listing(status="pending", id=1),
            _listing(status="approved", id=2),
            _listing(status="rejected", id=3),
        ]
        _patch_listings(monkeypatch, ADMIN, FakeSession(rows))

        assert [item["id"] for item in listings.list_all(status="pending")] == [1]
        assert [item["id"] for item in listings.list_all(status="rejected")] == [3]
        assert listings.list_all(status="mystery") == []


class TestTransitionAuthorization:
    def test_unknown_actor_is_forbidden(self, monkeypatch):
        listing = _listing()
        session = _patch_listings(monkeypatch, ADMIN, FakeSession([listing]))
        monkeypatch.setattr(listings, "get_user", lambda user_id: None)

        result = listings.transition(ADMIN["id"], listing.id, "verify")

        assert result == {
            "ok": False,
            "code": "forbidden",
            "error": "unknown user",
        }
        assert session.commits == 0

    def test_unknown_action_is_rejected_before_state_change(self, monkeypatch):
        listing = _listing()
        session = _patch_listings(monkeypatch, ADMIN, FakeSession([listing]))

        result = listings.transition(ADMIN["id"], listing.id, "destroy")

        assert result["ok"] is False
        assert result["code"] == "invalid_action"
        assert "action must be one of" in result["error"]
        assert session.commits == 0
        assert listing.status == "pending"

    def test_missing_listing_returns_not_found(self, monkeypatch):
        session = _patch_listings(monkeypatch, ADMIN, FakeSession([]))

        result = listings.transition(ADMIN["id"], 10, "verify")

        assert result == {
            "ok": False,
            "code": "not_found",
            "error": "listing not found",
        }
        assert session.commits == 0

    @pytest.mark.parametrize("action", ["verify", "approve", "reject"])
    @pytest.mark.parametrize("actor", [OWNER, OTHER_LANDLORD, STUDENT])
    def test_admin_only_actions_forbid_non_admins(self, monkeypatch, action, actor):
        listing = _listing()
        session = _patch_listings(monkeypatch, actor, FakeSession([listing]))

        result = listings.transition(actor["id"], listing.id, action)

        assert result["ok"] is False
        assert result["code"] == "forbidden"
        assert result["error"] == "admin only"
        assert listing.status == "pending"
        assert session.commits == 0

    @pytest.mark.parametrize("action", ["activate", "deactivate", "resubmit", "submit"])
    def test_owner_actions_forbid_non_owner_landlord(self, monkeypatch, action):
        listing = _listing(status="approved")
        session = _patch_listings(monkeypatch, OTHER_LANDLORD, FakeSession([listing]))

        result = listings.transition(OTHER_LANDLORD["id"], listing.id, action)

        assert result["ok"] is False
        assert result["code"] == "forbidden"
        assert result["error"] == "owner or admin only"
        assert listing.status == "approved"
        assert session.commits == 0

    @pytest.mark.parametrize("action", ["activate", "deactivate", "resubmit", "submit"])
    def test_owner_actions_forbid_student(self, monkeypatch, action):
        listing = _listing(status="approved")
        session = _patch_listings(monkeypatch, STUDENT, FakeSession([listing]))

        result = listings.transition(STUDENT["id"], listing.id, action)

        assert result["ok"] is False
        assert result["code"] == "forbidden"
        assert result["error"] == "owner or admin only"
        assert session.commits == 0

    def test_owner_can_activate_own_listing(self, monkeypatch):
        listing = _listing(status="approved")
        session = _patch_listings(monkeypatch, OWNER, FakeSession([listing]))

        result = listings.transition(OWNER["id"], listing.id, "activate")

        assert result["ok"] is True
        assert result["listing"]["status"] == "active"
        assert result["listing"]["flag_active"] == 1
        assert listing.status == "active"
        assert session.commits == 1

    def test_admin_can_activate_listing_owned_by_someone_else(self, monkeypatch):
        listing = _listing(status="approved")
        session = _patch_listings(monkeypatch, ADMIN, FakeSession([listing]))

        result = listings.transition(ADMIN["id"], listing.id, "activate")

        assert result["ok"] is True
        assert listing.status == "active"
        assert session.commits == 1

    @pytest.mark.parametrize("action", ["verify", "approve", "reject"])
    def test_admin_can_run_admin_only_actions(self, monkeypatch, action):
        listing = _listing()
        session = _patch_listings(monkeypatch, ADMIN, FakeSession([listing]))

        result = listings.transition(ADMIN["id"], listing.id, action)

        assert result["ok"] is True
        assert session.commits == 1

    def test_owner_activation_from_pending_is_invalid_transition(self, monkeypatch):
        listing = _listing(status="pending")
        session = _patch_listings(monkeypatch, OWNER, FakeSession([listing]))

        result = listings.transition(OWNER["id"], listing.id, "activate")

        assert result["ok"] is False
        assert result["code"] == "invalid_transition"
        assert "invalid transition" in result["error"]
        assert listing.status == "pending"
        assert session.commits == 0

    def test_unknown_status_reports_invalid_state_code(self, monkeypatch):
        listing = _listing(status="mystery")
        session = _patch_listings(monkeypatch, ADMIN, FakeSession([listing]))

        result = listings.transition(ADMIN["id"], listing.id, "verify")

        assert result["ok"] is False
        assert result["code"] == "invalid_state"
        assert "unknown listing status" in result["error"]
        assert session.commits == 0

    def test_transition_rejects_unauthorized_before_checking_validity(self, monkeypatch):
        listing = _listing(status="active")
        session = _patch_listings(monkeypatch, OTHER_LANDLORD, FakeSession([listing]))

        result = listings.transition(OTHER_LANDLORD["id"], listing.id, "verify")

        assert result["code"] == "forbidden"
        assert session.commits == 0


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


class TestApiAuthDependency:
    def test_resolves_identity_from_valid_bearer_token(self, test_session, monkeypatch):
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
        ["not-a-jwt", "", "eyJhbGciOiJIUzI1NiJ9.invalid.signature"],
    )
    def test_rejects_malformed_bearer_token(self, test_session, monkeypatch, token):
        _patch_auth_settings(monkeypatch, "production")

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                get_api_user(_request({"Authorization": f"Bearer {token}"}), test_session)
            )

        assert exc_info.value.status_code == 401
        assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}

    def test_rejects_token_with_forged_signature(self, test_session, monkeypatch):
        _patch_auth_settings(monkeypatch, "production")
        token = jwt.encode(
            {"sub": "1"}, "a-completely-different-secret-key-123", algorithm="HS256"
        )

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                get_api_user(_request({"Authorization": f"Bearer {token}"}), test_session)
            )

        assert exc_info.value.status_code == 401

    def test_rejects_non_integer_subject(self, test_session, monkeypatch):
        _patch_auth_settings(monkeypatch, "production")
        token = create_access_token({"sub": "not-an-int", "role": "student"})

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                get_api_user(_request({"Authorization": f"Bearer {token}"}), test_session)
            )

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Could not validate credentials"

    def test_rejects_subject_with_no_matching_user(self, test_session, monkeypatch):
        _patch_auth_settings(monkeypatch, "production")
        token = create_access_token({"sub": "987654", "role": "student"})

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                get_api_user(_request({"Authorization": f"Bearer {token}"}), test_session)
            )

        assert exc_info.value.status_code == 401

    def test_rejects_empty_bearer_value(self, test_session, monkeypatch):
        _patch_auth_settings(monkeypatch, "production")

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_api_user(_request({"Authorization": "Bearer "}), test_session))

        assert exc_info.value.status_code == 401

    def test_accepts_legacy_header_only_in_development(self, test_session, monkeypatch):
        user = _make_user(test_session, "legacy-student", "student")
        headers = {"X-User-Id": str(user.id)}

        _patch_auth_settings(monkeypatch, "development")
        assert asyncio.run(get_api_user(_request(headers), test_session)).id == user.id

        _patch_auth_settings(monkeypatch, "production")
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_api_user(_request(headers), test_session))
        assert exc_info.value.status_code == 401

    def test_rejects_non_integer_legacy_header(self, test_session, monkeypatch):
        _patch_auth_settings(monkeypatch, "development")

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_api_user(_request({"X-User-Id": "abc"}), test_session))

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "X-User-Id must be int"

    def test_rejects_unknown_legacy_user(self, test_session, monkeypatch):
        _patch_auth_settings(monkeypatch, "development")

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_api_user(_request({"X-User-Id": "987654"}), test_session))

        assert exc_info.value.status_code == 401
        assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}

    def test_ignores_legacy_header_when_bearer_is_present(self, test_session, monkeypatch):
        user = _make_user(test_session, "mixed-auth", "student")
        _patch_auth_settings(monkeypatch, "development")
        token = create_access_token({"sub": str(user.id), "role": "student"})

        resolved = asyncio.run(
            get_api_user(
                _request(
                    {"Authorization": f"Bearer {token}", "X-User-Id": "987654"}
                ),
                test_session,
            )
        )

        assert resolved.id == user.id


class TestAuthRoleHelpers:
    @pytest.mark.parametrize(
        ("role", "expected"), [("admin", True), ("student", False), ("", False)]
    )
    def test_is_admin(self, role, expected):
        assert asyncio.run(is_admin(SimpleNamespace(role=role))) is expected

    @pytest.mark.parametrize(("dependency", "code"), [(is_admin_dep, 401), (is_api_admin_dep, 403)])
    def test_non_admin_dependencies_reject_with_expected_status(self, dependency, code):
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(dependency(SimpleNamespace(role="student")))
        assert exc_info.value.status_code == code

    def test_admin_dependencies_return_the_user(self):
        user = SimpleNamespace(role="admin")

        assert asyncio.run(is_admin_dep(user)) is user
        assert asyncio.run(is_api_admin_dep(user)) is user


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ({"role": "student", "student_id": "202012345", "provisional": 0}, True),
        ({"role": "student", "student_id": None, "provisional": 1}, True),
        ({"role": "student", "student_id": None, "provisional": 0}, False),
        ({"role": "student", "student_id": "   ", "provisional": 0}, False),
        ({"role": "landlord", "student_id": "202012345", "provisional": 0}, False),
        (None, False),
    ],
)
def test_is_eligible_student(user, expected):
    assert is_eligible_student(user) is expected


class TestSupportUtilities:
    def test_public_user_strips_secrets_from_dict(self):
        result = public_user(
            {"id": 1, "username": "u", "password": "secret", "password_hash": "hash"}
        )

        assert result == {"id": 1, "username": "u"}

    def test_public_user_strips_password_from_model(self):
        user = User(username="u", email="u@example.com", password="secret", role="student")

        result = public_user(user)

        assert "password" not in result
        assert result["username"] == "u"
        assert result["role"] == "student"

    def test_public_user_returns_none_for_none(self):
        assert public_user(None) is None

    def test_flash_messages_are_accumulated_then_cleared(self):
        request = SimpleNamespace(session={})
        flash(request, "Saved", "success")
        flash(request, "Try again", "danger")

        assert request.session["_messages"] == [
            {"message": "Saved", "type": "success"},
            {"message": "Try again", "type": "danger"},
        ]
        messages = get_flashed_messages(request)
        assert messages == [
            {"message": "Saved", "type": "success"},
            {"message": "Try again", "type": "danger"},
        ]
        assert get_flashed_messages(request) == []

    def test_get_flashed_messages_returns_empty_list_when_none_stored(self):
        assert get_flashed_messages(SimpleNamespace(session={})) == []

    def test_pagination_calculates_pages_and_neighbours(self):
        pagination = Pagination(total_count=25, current_page=2, limit=10)

        assert pagination.total_pages == 3
        assert pagination.page == 2
        assert pagination.has_prev is True
        assert pagination.prev_num == 1
        assert pagination.has_next is True
        assert pagination.next_num == 3

    def test_pagination_boundaries(self):
        first = Pagination(total_count=10, current_page=1, limit=10)
        last = Pagination(total_count=10, current_page=3, limit=5)

        assert first.has_prev is False
        assert first.has_next is False
        assert last.has_prev is True
        assert last.has_next is False

    def test_pagination_iter_pages_marks_gaps(self):
        assert list(Pagination(10, 5, 1).iter_pages()) == [
            1, 2, 3, 4, 5, 6, 7, None, 9, 10
        ]
        assert list(Pagination(3, 1, 10).iter_pages()) == [1]

    def test_pagination_with_zero_limit(self):
        pagination = Pagination(10, 1, 0)

        assert pagination.total_pages == 1
        assert pagination.has_next is False

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("", "(unset)"),
            (None, "(unset)"),
            ("abcd", "****"),
            ("abcdefgh", "****efgh"),
        ],
    )
    def test_mask_secret_hides_sensitive_values(self, value, expected):
        assert mask_secret(value) == expected

    @pytest.mark.parametrize(
        ("uri", "expected"),
        [
            (
                "postgresql://alice:s3cret@db:5432/app",
                "postgresql://alice:****@db:5432/app",
            ),
            ("sqlite:///database.db", "sqlite:///database.db"),
        ],
    )
    def test_mask_database_uri_hides_password_only(self, uri, expected):
        assert mask_database_uri(uri) == expected

    @pytest.mark.parametrize(
        ("uri", "expected"),
        [
            ("postgres://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
            ("postgresql://u:p@h/db", "postgresql+psycopg2://u:p@h/db"),
            ("sqlite:///database.db", "sqlite:///database.db"),
        ],
    )
    def test_sqlalchemy_uri_rewrites_render_urls(self, uri, expected):
        assert sqlalchemy_uri(uri) == expected

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (ValueError("nope"), False),
            (RuntimeError("no such table: user"), True),
            (RuntimeError("relation \"user\" does not exist"), True),
            (RuntimeError("connection refused"), True),
        ],
    )
    def test_is_db_not_ready_error_matches_known_markers(self, error, expected):
        assert is_db_not_ready_error(error) is expected

    @pytest.mark.parametrize("error_type", [OperationalError, ProgrammingError])
    def test_is_db_not_ready_error_matches_sqlalchemy_errors(self, error_type):
        error = error_type("SELECT 1", {}, Exception("boom"))

        assert is_db_not_ready_error(error) is True

    def test_set_runtime_env_normalizes_and_persists(self, monkeypatch):
        monkeypatch.setenv("ENV", "production")
        calls = []
        monkeypatch.setattr(
            "app.config.clear_settings_cache", lambda: calls.append("cache")
        )
        monkeypatch.setattr(
            "app.config._upsert_dotenv", lambda key, value: calls.append((key, value))
        )

        result = set_runtime_env(" DEV ")

        assert result == "development"
        assert get_settings() is not None
        assert calls == ["cache", ("ENV", "development")]

    def test_set_runtime_env_rejects_unknown_mode(self, monkeypatch):
        monkeypatch.setenv("ENV", "production")
        calls = []
        monkeypatch.setattr(
            "app.config.clear_settings_cache", lambda: calls.append("cache")
        )
        monkeypatch.setattr(
            "app.config._upsert_dotenv", lambda key, value: calls.append((key, value))
        )

        with pytest.raises(ValueError, match="ENV must be production or development"):
            set_runtime_env("staging")

        assert calls == []
