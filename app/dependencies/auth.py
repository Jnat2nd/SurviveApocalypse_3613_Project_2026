from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from jwt.exceptions import InvalidTokenError

from app.config import get_settings
from app.dependencies.session import SessionDep
from app.models.user import User
from app.repositories.user import UserRepository


async def get_current_user(request: Request, db: SessionDep) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = request.cookies.get("access_token")

    if token is None:
        raise credentials_exception
    try:
        payload = jwt.decode(
            token,
            get_settings().secret_key,
            algorithms=[get_settings().jwt_algorithm],
        )
        user_id = payload.get("sub", None)
    except InvalidTokenError:
        raise credentials_exception

    repo = UserRepository(db)
    user = repo.get_by_id(user_id)

    if user is None:
        raise credentials_exception
    return user


def _dev_legacy_header_allowed() -> bool:
    return get_settings().env.lower() in {"dev", "development"}


async def get_api_user(request: Request, db: SessionDep) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    repo = UserRepository(db)

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.removeprefix("Bearer ").strip()
        if not token:
            raise credentials_exception
        try:
            payload = jwt.decode(
                token,
                get_settings().secret_key,
                algorithms=[get_settings().jwt_algorithm],
            )
            sub = payload.get("sub")
        except InvalidTokenError:
            raise credentials_exception

        try:
            user_id = int(sub)
        except (TypeError, ValueError):
            raise credentials_exception

        user = repo.get_by_id(user_id)
        if user is None:
            raise credentials_exception
        return user

    if _dev_legacy_header_allowed():
        x_user_id = request.headers.get("X-User-Id")
        if x_user_id:
            try:
                user_id = int(x_user_id)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="X-User-Id must be int") from exc
            user = repo.get_by_id(user_id)
            if user is None:
                raise credentials_exception
            return user

    raise credentials_exception


async def is_logged_in(request: Request, db: SessionDep):
    try:
        await get_current_user(request, db)
        return True
    except Exception:
        return False


IsUserLoggedIn = Annotated[bool, Depends(is_logged_in)]
AuthDep = Annotated[User, Depends(get_current_user)]
ApiUserDep = Annotated[User, Depends(get_api_user)]


async def is_admin(user: User):
    return user.role == "admin"


async def is_admin_dep(user: AuthDep):
    if not await is_admin(user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="You are not authorized to access this page",
        )
    return user


async def is_api_admin_dep(user: ApiUserDep):
    if not await is_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin only",
        )
    return user


AdminDep = Annotated[User, Depends(is_admin_dep)]
ApiAdminDep = Annotated[User, Depends(is_api_admin_dep)]
