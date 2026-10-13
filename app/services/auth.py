import logging
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.models.user import Account, User

logger = logging.getLogger(__name__)

SECRET_KEY = "tam-thoi-dung-khoa-nay-cho-dev-32bytes"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24


def verify_credentials(identifier: str, password: str, db: Session | None = None) -> dict:
    normalized = identifier.strip().lower()
    if not normalized.endswith("@academic.edu.vn") and "@" not in normalized:
        normalized += "@academic.edu.vn"

    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database service unavailable.",
        )

    try:
        user = db.query(User).filter(func.lower(User.email) == normalized).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password."
            )

        if user.status != "ACTIVE":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Account is {user.status.lower()}.",
            )

        account = (
            db.query(Account)
            .filter(Account.user_id == user.id, Account.is_active.is_(True))
            .first()
        )
        if not account or not verify_password(password, account.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password."
            )

        # Update last_login_at
        user.last_login_at = datetime.now(UTC)
        db.commit()

        role_name = user.role.name.lower() if user.role else "student"
        return {"identifier": user.email, "role": role_name}
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        logger.error("Database error during authentication: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal database error during authentication.",
        ) from exc


def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(UTC) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})

    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
