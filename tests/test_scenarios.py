"""Scenario tests: realistic multi-step workflows for each application role."""

import pytest
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


def test_provisional_student_can_browse_and_message():
    headers = _headers_for("incoming@example.com", "stud-dev")
    browse_response = client.get("/api/student/listings", headers=headers)

    assert browse_response.status_code == 200
    active_listing = _admin_listing("Verified Active Flat")
    assert any(
        listing["id"] == active_listing["id"]
        for listing in browse_response.json()["listings"]
    )

    message_response = client.post(
        "/api/student/messages",
        json={"listing_id": active_listing["id"], "body": "Is this available?"},
        headers=headers,
    )
    assert message_response.status_code == 200
    assert message_response.json()["ok"] is True


def test_student_without_id_or_provisional_flag_cannot_browse_or_message(monkeypatch):
    headers = _headers_for("student@example.com", "stud-dev")
    monkeypatch.setattr(
        "app.services.listings.get_user",
        lambda _user_id: {
            "id": int(headers["X-User-Id"]),
            "role": "student",
            "student_id": None,
            "provisional": 0,
        },
    )

    browse_response = client.get("/api/student/listings", headers=headers)
    assert browse_response.status_code == 403
    assert browse_response.json()["detail"] == "eligible students only"

    active_listing = _admin_listing("Verified Active Flat")
    message_response = client.post(
        "/api/student/messages",
        json={"listing_id": active_listing["id"], "body": "Is this available?"},
        headers=headers,
    )
    assert message_response.status_code == 400
    assert message_response.json()["detail"] == "eligible students only"


def test_student_browse_amenity_filter_uses_exact_case_insensitive_tokens():
    headers = _headers_for("student@example.com", "stud-dev")

    case_insensitive = client.get(
        "/api/student/listings",
        params={"amenity": "WiFi, Parking"},
        headers=headers,
    )
    assert case_insensitive.status_code == 200
    assert "Verified Active Flat" in [
        item["title"] for item in case_insensitive.json()["listings"]
    ]

    partial_token = client.get(
        "/api/student/listings",
        params={"amenity": "wi"},
        headers=headers,
    )
    assert partial_token.status_code == 200
    assert partial_token.json()["listings"] == []


def test_student_browse_amenity_comma_list_requires_every_token():
    headers = _headers_for("student@example.com", "stud-dev")

    response = client.get(
        "/api/student/listings",
        params={"amenity": "WiFi, Gym"},
        headers=headers,
    )

    assert response.status_code == 200
    titles = [item["title"] for item in response.json()["listings"]]
    assert "Summit Hill Apartment" in titles
    assert "Verified Active Flat" not in titles


def test_student_browse_min_price_is_inclusive_and_applies_with_max_price():
    headers = _headers_for("student@example.com", "stud-dev")

    response = client.get(
        "/api/student/listings",
        params={"min_price": 3500, "max_price": 3500},
        headers=headers,
    )

    assert response.status_code == 200
    listings = response.json()["listings"]
    assert listings
    assert all(item["price"] == 3500 for item in listings)
    assert "Verified Active Flat" in [item["title"] for item in listings]


def test_student_browse_rejects_negative_or_inverted_price_ranges():
    headers = _headers_for("student@example.com", "stud-dev")

    negative_minimum = client.get(
        "/api/student/listings",
        params={"min_price": -1},
        headers=headers,
    )
    assert negative_minimum.status_code == 422

    inverted_range = client.get(
        "/api/student/listings",
        params={"min_price": 3500, "max_price": 2500},
        headers=headers,
    )
    assert inverted_range.status_code == 422


@pytest.fixture
def approved_listing(api):
    listing = api.submit("Approval Ready Unit")
    response = api.transition(listing["id"], "verify", api.headers_for("admin"))
    assert response.status_code == 200
    return listing


class TestLandlordWorkflows:
    def test_landlord_publishes_listing_and_student_contacts_then_it_is_taken_down(
        self, api
    ):
        listing = api.submit(
            "Scenario Happy Path",
            area="mount_hope",
            price=1800,
            amenities="wifi,laundry",
        )

        verified = api.transition(listing["id"], "verify", api.headers_for("admin"))
        assert verified.status_code == 200
        assert verified.json()["listing"]["status"] == "approved"
        assert "Scenario Happy Path" not in [
            item["title"] for item in api.browse("student")
        ]

        activated = api.transition(listing["id"], "activate", api.headers_for("landlord"))
        assert activated.status_code == 200
        assert api.status(listing["id"]) == "active"

        visible = api.browse("student", area="mount_hope", max_price=2000, amenity="laundry")
        assert [item["title"] for item in visible] == ["Scenario Happy Path"]

        message = api.client.post(
            "/api/student/messages",
            json={"listing_id": listing["id"], "body": "Is it available?"},
            headers=api.headers_for("student"),
        )
        assert message.status_code == 200
        assert isinstance(message.json()["message_id"], int)

        deactivated = api.transition(
            listing["id"], "deactivate", api.headers_for("landlord")
        )
        assert deactivated.status_code == 200
        assert api.status(listing["id"]) == "inactive"
        assert "Scenario Happy Path" not in [
            item["title"] for item in api.browse("student")
        ]

        failed_message = api.client.post(
            "/api/student/messages",
            json={"listing_id": listing["id"], "body": "Still there?"},
            headers=api.headers_for("student"),
        )
        assert failed_message.status_code == 400
        assert failed_message.json()["detail"] == "listing not available for messaging"

    def test_rejected_listing_can_be_resubmitted_and_republished(self, api):
        listing = api.submit("Scenario Reject Resubmit", area="st_augustine", price=1500)

        rejected = api.transition(listing["id"], "reject", api.headers_for("admin"))
        assert rejected.status_code == 200
        assert api.status(listing["id"]) == "rejected"
        assert api.admin_listing("Scenario Reject Resubmit", status="rejected")["id"] == listing["id"]
        assert "Scenario Reject Resubmit" not in [
            item["title"] for item in api.browse("student")
        ]

        blocked = api.client.post(
            "/api/student/messages",
            json={"listing_id": listing["id"], "body": "Hello?"},
            headers=api.headers_for("student"),
        )
        assert blocked.status_code == 400

        resubmitted = api.transition(
            listing["id"], "resubmit", api.headers_for("landlord")
        )
        assert resubmitted.status_code == 200
        assert api.status(listing["id"]) == "pending"

        assert api.transition(listing["id"], "verify", api.headers_for("admin")).status_code == 200
        assert api.transition(listing["id"], "activate", api.headers_for("landlord")).status_code == 200
        assert "Scenario Reject Resubmit" in [
            item["title"] for item in api.browse("student")
        ]

    def test_non_landlord_cannot_publish_and_overpriced_listing_is_rejected(self, api):
        before = api.admin_listing("Verified Active Flat")["id"]
        listing_count = len(
            api.client.get(
                "/api/admin/listings", headers=api.headers_for("admin")
            ).json()["listings"]
        )

        student_attempt = api.client.post(
            "/api/landlord/listings",
            json={
                "title": "Student Publish Attempt",
                "area": "st_augustine",
                "price": 1000,
                "contact_phone": "555-0100",
                "contact_email": "student@example.com",
            },
            headers=api.headers_for("student"),
        )
        assert student_attempt.status_code == 400
        assert student_attempt.json()["detail"] == "landlords only"

        price_attempt = api.client.post(
            "/api/landlord/listings",
            json={
                "title": "Free Unit",
                "area": "st_augustine",
                "price": 0,
                "contact_phone": "555-0100",
                "contact_email": "landlord@example.com",
            },
            headers=api.headers_for("landlord"),
        )
        assert price_attempt.status_code == 400
        assert price_attempt.json()["detail"] == "price must be positive"

        after = api.client.get(
            "/api/admin/listings", headers=api.headers_for("admin")
        ).json()["listings"]
        assert len(after) == listing_count
        assert api.admin_listing("Verified Active Flat")["id"] == before


class TestAdminReviewWorkflows:
    def test_verify_and_approve_both_publish_a_listing_for_review(self, api):
        first = api.submit("Scenario Verified")
        second = api.submit("Scenario Approved")

        verified = api.transition(first["id"], "verify", api.headers_for("admin"))
        approved = api.transition(second["id"], "approve", api.headers_for("admin"))

        assert verified.json()["listing"]["status"] == "approved"
        assert approved.json()["listing"]["status"] == "approved"
        approved_titles = {
            item["title"]
            for item in api.client.get(
                "/api/admin/listings",
                params={"status": "approved"},
                headers=api.headers_for("admin"),
            ).json()["listings"]
        }
        legacy_titles = {
            item["title"]
            for item in api.client.get(
                "/api/admin/listings",
                params={"status": "verified"},
                headers=api.headers_for("admin"),
            ).json()["listings"]
        }
        assert {"Scenario Verified", "Scenario Approved"} <= approved_titles
        assert {"Scenario Verified", "Scenario Approved"} <= legacy_titles

    def test_owner_and_student_cannot_verify_but_admin_can(self, api):
        listing = api.submit("Scenario Verify Gate")

        owner_attempt = api.transition(
            listing["id"], "verify", api.headers_for("landlord")
        )
        student_attempt = api.transition(
            listing["id"], "verify", api.headers_for("student")
        )
        assert owner_attempt.status_code == 403
        assert student_attempt.status_code == 403
        assert owner_attempt.json()["detail"] == "admin only"
        assert api.status(listing["id"]) == "pending"

        assert api.transition(listing["id"], "verify", api.headers_for("admin")).status_code == 200
        assert api.status(listing["id"]) == "approved"

    def test_admin_can_reject_an_approved_listing(self, api, approved_listing):
        response = api.transition(
            approved_listing["id"], "reject", api.headers_for("admin")
        )

        assert response.status_code == 200
        assert api.status(approved_listing["id"]) == "rejected"

        resubmitted = api.transition(
            approved_listing["id"], "resubmit", api.headers_for("landlord")
        )
        assert resubmitted.status_code == 200
        assert api.status(approved_listing["id"]) == "pending"

    def test_admin_can_activate_a_listing_owned_by_someone_else(self, api, approved_listing):
        response = api.transition(
            approved_listing["id"], "activate", api.headers_for("admin")
        )

        assert response.status_code == 200
        assert api.status(approved_listing["id"]) == "active"
        assert "Approval Ready Unit" in [
            item["title"] for item in api.browse("student")
        ]


class TestOwnershipWorkflows:
    def test_other_landlord_and_student_cannot_activate(self, api, approved_listing):
        other_attempt = api.transition(
            approved_listing["id"], "activate", api.headers_for("landlord2")
        )
        student_attempt = api.transition(
            approved_listing["id"], "activate", api.headers_for("student")
        )
        assert other_attempt.status_code == 403
        assert student_attempt.status_code == 403
        assert other_attempt.json()["detail"] == "owner or admin only"
        assert api.status(approved_listing["id"]) == "approved"

        owner_attempt = api.transition(
            approved_listing["id"], "activate", api.headers_for("landlord")
        )
        assert owner_attempt.status_code == 200
        assert api.status(approved_listing["id"]) == "active"

    def test_other_landlord_cannot_deactivate_an_active_listing(self, api, approved_listing):
        assert api.transition(
            approved_listing["id"], "activate", api.headers_for("landlord")
        ).status_code == 200

        other_attempt = api.transition(
            approved_listing["id"], "deactivate", api.headers_for("landlord2")
        )
        assert other_attempt.status_code == 403
        assert api.status(approved_listing["id"]) == "active"

        owner_attempt = api.transition(
            approved_listing["id"], "deactivate", api.headers_for("landlord")
        )
        assert owner_attempt.status_code == 200
        assert api.status(approved_listing["id"]) == "inactive"

    def test_other_landlord_cannot_resubmit_a_rejected_listing(self, api):
        listing = api.submit("Scenario Resubmit Gate")
        assert api.transition(listing["id"], "verify", api.headers_for("admin")).status_code == 200
        assert api.transition(listing["id"], "reject", api.headers_for("admin")).status_code == 200

        other_attempt = api.transition(
            listing["id"], "resubmit", api.headers_for("landlord2")
        )
        assert other_attempt.status_code == 403
        assert api.status(listing["id"]) == "rejected"

        owner_attempt = api.transition(
            listing["id"], "resubmit", api.headers_for("landlord")
        )
        assert owner_attempt.status_code == 200
        assert api.status(listing["id"]) == "pending"


class TestStudentWorkflows:
    def test_student_filters_visible_listings(self, api):
        cheap = api.submit(
            "Filter Cheap", area="st_augustine", price=1000, amenities="wifi,gym"
        )
        expensive = api.submit(
            "Filter Expensive", area="mount_hope", price=4000, amenities="wifi,parking"
        )
        admin = api.headers_for("admin")
        for listing in (cheap, expensive):
            assert api.transition(listing["id"], "verify", admin).status_code == 200
            assert api.transition(listing["id"], "activate", admin).status_code == 200

        by_area = [item["title"] for item in api.browse("student", area="st_augustine")]
        assert "Filter Cheap" in by_area
        assert "Filter Expensive" not in by_area

        by_price = [item["title"] for item in api.browse("student", max_price=2000)]
        assert "Filter Cheap" in by_price
        assert "Filter Expensive" not in by_price

        by_amenity = [item["title"] for item in api.browse("student", amenity="gym")]
        assert "Filter Cheap" in by_amenity
        assert "Filter Expensive" not in by_amenity

        combined = [
            item["title"]
            for item in api.browse(
                "student", area="mount_hope", max_price=2000, amenity="gym"
            )
        ]
        assert combined == []

    def test_provisional_student_can_currently_browse_and_message(self, api):
        listings = api.browse("incoming")

        assert "Verified Active Flat" in [item["title"] for item in listings]
        active = next(item for item in listings if item["title"] == "Verified Active Flat")
        response = api.client.post(
            "/api/student/messages",
            json={"listing_id": active["id"], "body": "Hello"},
            headers=api.headers_for("incoming"),
        )
        assert response.status_code == 200

    def test_empty_message_body_is_rejected(self, api):
        active = next(
            item for item in api.browse("student") if item["title"] == "Verified Active Flat"
        )

        response = api.client.post(
            "/api/student/messages",
            json={"listing_id": active["id"], "body": "   "},
            headers=api.headers_for("student"),
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "body required"

    def test_landlord_cannot_use_student_features(self, api):
        response = api.client.get(
            "/api/student/listings", headers=api.headers_for("landlord")
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "students only"

        message = api.client.post(
            "/api/student/messages",
            json={"listing_id": 1, "body": "Hello"},
            headers=api.headers_for("landlord"),
        )
        assert message.status_code == 400
        assert message.json()["detail"] == "students only"


class TestErrorRecoveryWorkflows:
    def test_failed_html_sign_in_can_be_retried_successfully(self, client):
        failed = client.post(
            "/login",
            data={"username": "admin", "password": "wrong"},
            follow_redirects=False,
        )
        assert failed.status_code == 303

        success = client.post(
            "/login",
            data={"username": "admin", "password": "admin-dev"},
            follow_redirects=False,
        )
        assert success.status_code == 303
        assert "access_token" in success.cookies

        admin_page = client.get("/admin", follow_redirects=False)
        assert admin_page.status_code == 200

        logout = client.get("/logout", follow_redirects=False)
        assert logout.status_code == 303
        assert client.get("/app", follow_redirects=False).status_code == 401
