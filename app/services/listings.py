"""Listing CRUD, browse filters, messaging, and lifecycle transitions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from app.database import engine
from app.models.listing import Listing
from app.models.message import Message
from app.services.housing_auth import get_user, is_eligible_student
from app.services.lifecycle import (
    ACTIONS,
    apply_action,
    fields_for_status,
    messageable,
    normalize_status,
    visible_to_students,
)

ALLOWED_AREAS = {"st_augustine", "mount_hope"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_amenities(raw: str) -> list[str]:
    return [a.strip().lower() for a in (raw or "").split(",") if a.strip()]


def submit_listing(
    landlord_id: int,
    *,
    title: str,
    area: str,
    price: int,
    amenities: str,
    contact_phone: str,
    contact_email: str,
    photos: str = "",
) -> dict[str, Any]:
    user = get_user(landlord_id)
    if not user or user["role"] != "landlord":
        return {"ok": False, "error": "landlords only"}
    # DEFECT: area not restricted to ALLOWED_AREAS
    if price <= 0:
        return {"ok": False, "error": "price must be positive"}
    # DEFECT: empty amenities allowed

    with Session(engine) as session:
        initial_fields = fields_for_status("pending")
        listing = Listing(
            landlord_id=landlord_id,
            title=title,
            area=area,
            price=price,
            amenities=amenities,
            contact_phone=contact_phone,
            contact_email=contact_email,
            photos=photos,
            **initial_fields,
            created_at=_now(),
        )
        session.add(listing)
        session.commit()
        session.refresh(listing)
        return {"ok": True, "listing": listing.model_dump()}


def transition(actor_id: int, listing_id: int, action: str) -> dict[str, Any]:
    actor = get_user(actor_id)
    if not actor:
        return {"ok": False, "code": "forbidden", "error": "unknown user"}
    if action not in ACTIONS:
        return {"ok": False, "code": "invalid_action", "error": f"action must be one of {ACTIONS}"}

    with Session(engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return {"ok": False, "code": "not_found", "error": "listing not found"}
        row = listing.model_dump()

        if action in {"verify", "approve", "reject"}:
            if actor["role"] != "admin":
                return {"ok": False, "code": "forbidden", "error": "admin only"}
        elif action in {"activate", "deactivate", "resubmit", "submit"}:
            if actor["role"] != "admin" and actor["id"] != row.get("landlord_id"):
                return {"ok": False, "code": "forbidden", "error": "owner or admin only"}
        else:
            return {"ok": False, "code": "invalid_action", "error": f"action must be one of {ACTIONS}"}

        result = apply_action(row, action)
        if not result.get("ok"):
            if "code" not in result:
                result = {**result, "code": "invalid_transition"}
            return result

        updated = result["row"]
        listing.status = updated["status"]
        listing.flag_pending = updated["flag_pending"]
        listing.flag_verified = updated["flag_verified"]
        listing.flag_active = updated["flag_active"]
        listing.flag_rejected = updated["flag_rejected"]
        session.add(listing)
        session.commit()
        session.refresh(listing)
        return {"ok": True, "listing": listing.model_dump()}


def browse_for_students(
    student_id: int,
    *,
    area: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    amenity: str | None = None,
) -> dict[str, Any]:
    student = get_user(student_id)
    if not is_eligible_student(student):
        error = (
            "eligible students only"
            if student and student.get("role") == "student"
            else "students only"
        )
        return {"ok": False, "error": error}

    if min_price is not None and min_price < 0:
        return {
            "ok": False,
            "code": "invalid_price_range",
            "error": "min_price must be non-negative",
        }
    if max_price is not None and max_price < 0:
        return {
            "ok": False,
            "code": "invalid_price_range",
            "error": "max_price must be non-negative",
        }
    if min_price is not None and max_price is not None and min_price > max_price:
        return {
            "ok": False,
            "code": "invalid_price_range",
            "error": "min_price must not exceed max_price",
        }

    required_amenities = set(_parse_amenities(amenity or ""))

    with Session(engine) as session:
        rows = session.exec(select(Listing).order_by(Listing.id)).all()
        items = [r.model_dump() for r in rows]

    results = []
    for item in items:
        if not visible_to_students(item):
            continue
        if area and item["area"] != area:
            continue
        if min_price is not None and int(item["price"]) < min_price:
            continue
        if max_price is not None and int(item["price"]) > max_price:
            continue
        available_amenities = set(_parse_amenities(item["amenities"]))
        if required_amenities and not required_amenities.issubset(
            available_amenities
        ):
            continue
        results.append(item)
    return {"ok": True, "listings": results}


def list_all(*, status: str | None = None) -> list[dict[str, Any]]:
    with Session(engine) as session:
        stmt = select(Listing).order_by(Listing.id)
        normalized_status = normalize_status(status) if status else None
        if normalized_status:
            if normalized_status == "approved":
                stmt = stmt.where(Listing.status.in_({"approved", "verified"}))
            else:
                stmt = stmt.where(Listing.status == normalized_status)
        return [r.model_dump() for r in session.exec(stmt).all()]


def send_message(student_id: int, listing_id: int, body: str) -> dict[str, Any]:
    student = get_user(student_id)
    if not is_eligible_student(student):
        error = (
            "eligible students only"
            if student and student.get("role") == "student"
            else "students only"
        )
        return {"ok": False, "error": error}
    if not body.strip():
        return {"ok": False, "error": "body required"}
    with Session(engine) as session:
        listing = session.get(Listing, listing_id)
        if listing is None:
            return {"ok": False, "error": "listing not found"}
        if not messageable(listing.model_dump()):
            return {"ok": False, "error": "listing not available for messaging"}
        message = Message(
            listing_id=listing_id,
            student_id=student_id,
            body=body,
            created_at=_now(),
        )
        session.add(message)
        session.commit()
        session.refresh(message)
        return {"ok": True, "message_id": message.id}
