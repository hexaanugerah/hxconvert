from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.errors import AuthError, ConflictError, ValidationAppError
from app.core.rate_limit import enforce_rate_limit
from app.core.security import (
    ACCESS_COOKIE,
    GUEST_COOKIE,
    create_access_token,
    get_current_user,
    get_optional_user,
    hash_password,
    verify_password,
)
from app.models.conversion import ConversionJob
from app.models.user import User
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    UpdateProfileRequest,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_auth_cookie(response: Response, token: str) -> None:
    is_production = settings.environment == "production"
    response.set_cookie(
        ACCESS_COOKIE,
        token,
        max_age=settings.access_token_expire_minutes * 60,
        httponly=True,
        samesite="lax" if is_production else "none",
        secure=is_production,
        path="/",
    )


def _claim_guest_jobs(db: Session, request: Request, user: User) -> None:
    guest_id = request.cookies.get(GUEST_COOKIE)
    if not guest_id:
        return
    db.execute(
        update(ConversionJob)
        .where(ConversionJob.guest_id == guest_id, ConversionJob.user_id.is_(None))
        .values(user_id=user.id, guest_id=None)
    )
    db.commit()


@router.post("/register", response_model=AuthResponse, status_code=201)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    _rate=Depends(enforce_rate_limit),
):
    existing = db.scalar(select(User).where(User.email == payload.email))
    if existing:
        raise ConflictError("An account with that email already exists.")
    user = User(name=payload.name, email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    _claim_guest_jobs(db, request, user)
    token = create_access_token(user.id)
    _set_auth_cookie(response, token)
    response.delete_cookie(GUEST_COOKIE)
    return AuthResponse(user=UserOut.model_validate(user), access_token=token)


@router.post("/login", response_model=AuthResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    _rate=Depends(enforce_rate_limit),
):
    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AuthError("Email or password is incorrect.")
    _claim_guest_jobs(db, request, user)
    token = create_access_token(user.id)
    _set_auth_cookie(response, token)
    response.delete_cookie(GUEST_COOKIE)
    return AuthResponse(user=UserOut.model_validate(user), access_token=token)


@router.post("/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.status_code = 204
    return response


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return UserOut.model_validate(user)


@router.patch("/me", response_model=UserOut)
def update_profile(
    payload: UpdateProfileRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.email and payload.email != user.email:
        existing = db.scalar(select(User).where(User.email == payload.email, User.id != user.id))
        if existing:
            raise ConflictError("That email is already in use.")
        user.email = payload.email
    if payload.name is not None:
        if len(payload.name) < 2:
            raise ValidationAppError("Name must be at least 2 characters.")
        user.name = payload.name
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/change-password", status_code=204)
def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise AuthError("Your current password is incorrect.")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return Response(status_code=204)


@router.get("/session", response_model=dict)
def session_info(user: User | None = Depends(get_optional_user)):
    return {
        "authenticated": user is not None,
        "user": UserOut.model_validate(user).model_dump(mode="json") if user else None,
        "anonymous_allowed": settings.allow_anonymous,
    }
