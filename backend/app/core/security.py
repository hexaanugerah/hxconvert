"""Password hashing, JWT handling and authentication dependencies."""
from datetime import datetime, timedelta, timezone
import uuid

import bcrypt
import jwt
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.user import User

ALGORITHM = "HS256"
ACCESS_COOKIE = "dc_access"
GUEST_COOKIE = "dc_guest"


class AuthError(Exception):
    def __init__(self, message: str = "Not authenticated"):
        self.message = message


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None


def _token_from_request(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.cookies.get(ACCESS_COOKIE)


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = _token_from_request(request)
    if not token:
        return None
    user_id = decode_access_token(token)
    if not user_id:
        return None
    return db.get(User, user_id)


def get_current_user(user: User | None = Depends(get_optional_user)) -> User:
    if user is None:
        raise AuthError()
    return user


def get_or_create_guest_id(request: Request, response=None) -> str | None:
    """Return the caller's anonymous session id, or None when anonymous use is disabled."""
    if not settings.allow_anonymous:
        return None
    guest_id = request.cookies.get(GUEST_COOKIE)
    if guest_id:
        try:
            uuid.UUID(guest_id)
            return guest_id
        except ValueError:
            pass
    guest_id = str(uuid.uuid4())
    if response is not None:
        # For development (HTTP), use SameSite=none without Secure to allow cross-origin cookies.
        # Browsers allow this on localhost. For production (HTTPS), use SameSite=lax with Secure.
        is_production = settings.environment == "production"
        # SameSite=None without Secure is INVALID and browsers drop the whole
        # cookie, which silently broke guest job access in development. The
        # frontend now always calls the API on the page's own host (see
        # lib/api.ts), so the pairing stays same-site and plain SameSite=Lax
        # works over HTTP in development and over HTTPS with Secure in
        # production (same domain behind the reverse proxy).
        response.set_cookie(
            GUEST_COOKIE,
            guest_id,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
            secure=is_production,
            path="/",
        )
    return guest_id
