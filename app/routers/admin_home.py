from fastapi import Request
from fastapi.responses import HTMLResponse

from app.dependencies.auth import AdminDep
from app.dependencies.session import SessionDep
from app.repositories.user import UserRepository
from app.services import listings as listing_service

from . import router, templates


@router.get("/admin", response_class=HTMLResponse)
async def admin_home_view(
    request: Request,
    user: AdminDep,
    db:SessionDep
):
    return templates.TemplateResponse(
        request=request, 
        name="admin.html",
        context={
            "user": user
        }
    )


@router.get("/admin/listings", response_class=HTMLResponse)
async def admin_listings_view(
    request: Request,
    user: AdminDep,
    db: SessionDep,
    status: str | None = None,
):
    user_repo = UserRepository(db)
    landlord_cache = {}
    rows = []

    for item in listing_service.list_all(status=status):
        landlord_id = item.get("landlord_id")
        if landlord_id not in landlord_cache:
            landlord_cache[landlord_id] = user_repo.get_by_id(landlord_id)
        landlord = landlord_cache[landlord_id]
        rows.append(
            {
                **item,
                "landlord": (
                    landlord.display_name or landlord.username if landlord else "Unknown"
                ),
                "landlord_username": landlord.username if landlord else "",
            }
        )

    return templates.TemplateResponse(
        request=request,
        name="admin_listings.html",
        context={
            "user": user,
            "listings": rows,
            "status": status,
        },
    )
