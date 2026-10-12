"""Auth endpoints — DB login (/api/auth) + legacy mock login (/auth, FE compat)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.deps import ensure_bootstrap_admin, get_current_user, get_db
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    MeResponse,
    MockLoginRequest,
    MockLoginResponse,
)
from app.services import auth_service
from app.services.auth import create_access_token as create_mock_token
from app.services.auth import verify_credentials

router = APIRouter(prefix="/api/auth", tags=["auth"])
mock_router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    ensure_bootstrap_admin(db)
    user = auth_service.authenticate(db, payload.email, payload.password)
    if user is None:
        # Generic error — never reveal whether the email exists (AI-25).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=auth_service.login_error()
        )
    return LoginResponse(
        access_token=auth_service.issue_token(user),
        role=user.role,
        user_id=user.id,
        username=user.username,
        class_id=user.class_id,
    )


@router.get("/me", response_model=MeResponse)
def me(current_user=Depends(get_current_user)):
    return MeResponse(
        id=current_user.id,
        username=current_user.username,
        email=current_user.email,
        role=current_user.role,
        class_id=current_user.class_id,
        is_active=current_user.is_active,
    )


@mock_router.post("/login", response_model=MockLoginResponse)
def mock_login(request: MockLoginRequest):
    """Legacy mock login kept for the FE login modal (pre-DB contract)."""
    # 1. Kiểm tra tài khoản giả lập
    user_data = verify_credentials(request.identifier, request.password)

    # 2. Tạo Token JWT
    token = create_mock_token(data={"sub": user_data["identifier"], "role": user_data["role"]})

    # 3. Trả về Frontend
    return MockLoginResponse(
        token=token,
        role=user_data["role"],
    )
