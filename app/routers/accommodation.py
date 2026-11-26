"""Student-accommodation scenario JSON API (OpenAPI /docs)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.dependencies.auth import ApiAdminDep, ApiUserDep
from app.services import housing_auth as auth
from app.services import listings
from app.utilities.security import create_access_token

router = APIRouter(tags=["student-accommodation"])


class LoginBody(BaseModel):
    email: str
    password: str


class ListingBody(BaseModel):
    title: str
    area: str
    price: int
    amenities: str = ""
    contact_phone: str
    contact_email: str
    photos: str = ""


class TransitionBody(BaseModel):
    action: str = Field(
        description="verify | approve | reject | activate | deactivate | resubmit"
    )


class MessageBody(BaseModel):
    listing_id: int
    body: str


@router.post("/auth/login")
def login(body: LoginBody) -> dict[str, Any]:
    user = auth.login(body.email, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="invalid credentials")
    access_token = create_access_token(
        data={"sub": str(user["id"]), "role": user.get("role", "")}
    )
    return {"user": user, "access_token": access_token, "token_type": "bearer"}


@router.post("/landlord/listings")
def submit(body: ListingBody, current_user: ApiUserDep) -> dict[str, Any]:
    result = listings.submit_listing(
        current_user.id,
        title=body.title,
        area=body.area,
        price=body.price,
        amenities=body.amenities,
        contact_phone=body.contact_phone,
        contact_email=body.contact_email,
        photos=body.photos,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@router.post("/listings/{listing_id}/transition")
def transition(
    listing_id: int,
    body: TransitionBody,
    current_user: ApiUserDep,
) -> dict[str, Any]:
    result = listings.transition(current_user.id, listing_id, body.action)
    if not result.get("ok"):
        code = result.get("code")
        status_code = 400
        if code == "forbidden":
            status_code = 403
        elif code == "not_found":
            status_code = 404
        raise HTTPException(status_code=status_code, detail=result.get("error"))
    return result


@router.get("/student/listings")
def browse(
    current_user: ApiUserDep,
    area: str | None = None,
    min_price: int | None = Query(default=None, ge=0),
    max_price: int | None = Query(default=None, ge=0),
    amenity: str | None = None,
) -> dict[str, Any]:
    result = listings.browse_for_students(
        current_user.id,
        area=area,
        min_price=min_price,
        max_price=max_price,
        amenity=amenity,
    )
    if not result.get("ok"):
        status_code = 422 if result.get("code") == "invalid_price_range" else 403
        raise HTTPException(status_code=status_code, detail=result.get("error"))
    return result


@router.get("/admin/listings")
def admin_list(
    _admin: ApiAdminDep,
    status: str | None = None,
) -> dict[str, Any]:
    return {"listings": listings.list_all(status=status)}


@router.post("/student/messages")
def message(body: MessageBody, current_user: ApiUserDep) -> dict[str, Any]:
    result = listings.send_message(current_user.id, body.listing_id, body.body)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result
