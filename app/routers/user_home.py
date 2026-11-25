from fastapi import Query, Request, status
from fastapi.responses import HTMLResponse

from app.dependencies.auth import AuthDep
from app.dependencies.session import SessionDep
from app.services import listings as listing_service

from . import router, templates


@router.get("/app", response_class=HTMLResponse)
async def user_home_view(
    request: Request,
    user: AuthDep,
    db:SessionDep
):
    return templates.TemplateResponse(
        request=request, 
        name="app.html",
        context={
            "user": user
        }
    )


@router.get("/app/listings", response_class=HTMLResponse)
async def student_listings_view(
    request: Request,
    user: AuthDep,
    area: str | None = None,
    min_price: int | None = Query(default=None, ge=0),
    max_price: int | None = Query(default=None, ge=0),
    amenity: str | None = None,
):
    result = listing_service.browse_for_students(
        user.id,
        area=area,
        min_price=min_price,
        max_price=max_price,
        amenity=amenity,
    )
    return templates.TemplateResponse(
        request=request,
        name="listings.html",
        context={
            "user": user,
            "listings": result.get("listings", []),
            "error": None if result.get("ok") else result.get("error"),
        },
        status_code=(
            status.HTTP_200_OK
            if result.get("ok")
            else (
                status.HTTP_422_UNPROCESSABLE_ENTITY
                if result.get("code") == "invalid_price_range"
                else status.HTTP_403_FORBIDDEN
            )
        ),
    )


@router.get("/landlord/listings", response_class=HTMLResponse)
async def landlord_listings_view(
    request: Request,
    user: AuthDep,
):
    if user.role != "landlord":
        return templates.TemplateResponse(
            request=request,
            name="landlord_listings.html",
            context={
                "user": user,
                "listings": [],
                "error": "landlords only",
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    rows = [
        item
        for item in listing_service.list_all()
        if item.get("landlord_id") == user.id
    ]
    return templates.TemplateResponse(
        request=request,
        name="landlord_listings.html",
        context={
            "user": user,
            "listings": rows,
            "error": None,
        },
    )
