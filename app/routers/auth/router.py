"""Auth endpoints — DB login (/api/auth) + legacy mock login (/auth, FE compat)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.deps import ensure_bootstrap_admin, get_current_user, get_db
from app.models.course import Class, Enrollment
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    MeResponse,
    MessageResponse,
    MockLoginRequest,
    MockLoginResponse,
    ProfileUpdateRequest,
)
from app.services import auth_service, user_service
from app.services.auth import create_access_token as create_mock_token
from app.services.auth import verify_credentials

router = APIRouter(prefix="/api/auth", tags=["auth"])
mock_router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    ensure_bootstrap_admin(db)
    user = auth_service.authenticate(db, str(payload.email), payload.password)
    if user is None:
        # Generic error — never reveal whether the email exists (AI-25).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=auth_service.login_error()
        )
    role_name = user_service.role_name_of(user) or "STUDENT"
    class_id = None
    owned = db.query(Class).filter(Class.trainer_id == user.id).first()
    if owned is not None:
        class_id = owned.id
    else:
        enrolled = (
            db.query(Enrollment)
            .filter(Enrollment.student_id == user.id, Enrollment.status == "ACTIVE")
            .first()
        )
        if enrolled is not None:
            class_id = enrolled.class_id
    return LoginResponse(
        access_token=auth_service.issue_token(user, db),
        role=role_name.lower(),
        user_id=user.id,
        username=user.full_name,
        class_id=class_id,
    )


@router.get("/me", response_model=MeResponse)
def me(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    role_name = user_service.role_name_of(current_user) or "STUDENT"
    class_id = None
    owned = db.query(Class).filter(Class.trainer_id == current_user.id).first()
    if owned is not None:
        class_id = owned.id
    else:
        enrolled = (
            db.query(Enrollment)
            .filter(Enrollment.student_id == current_user.id, Enrollment.status == "ACTIVE")
            .first()
        )
        if enrolled is not None:
            class_id = enrolled.class_id
    return MeResponse(
        id=current_user.id,
        username=current_user.full_name,
        email=current_user.email,
        role=role_name.lower(),
        class_id=class_id,
        is_active=current_user.status == "ACTIVE",
    )


@router.patch("/profile", response_model=MeResponse)
def update_profile(
    payload: ProfileUpdateRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """AC-03: update the caller's own display name; FE re-reads /me after."""
    try:
        user = auth_service.update_profile(db, current_user, payload.full_name)
    except auth_service.ProfileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return me(current_user=user, db=db)


@router.post("/change-password", response_model=MessageResponse)
def change_password(
    payload: ChangePasswordRequest,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """AC-02: verify old password, store the new one hashed (bcrypt)."""
    try:
        auth_service.change_password(db, current_user, payload.old_password, payload.new_password)
    except auth_service.ProfileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return MessageResponse(message="Password changed successfully.")


@mock_router.post("/login", response_model=MockLoginResponse)
def mock_login(request: MockLoginRequest, db: Session = Depends(get_db)):
    # 1. Kiểm tra tài khoản giả lập
    user_data = verify_credentials(request.identifier, request.password, db)

    # 2. Tạo Token JWT
    token = create_mock_token(data={"sub": user_data["identifier"], "role": user_data["role"]})

    # 3. Trả về Frontend
    return MockLoginResponse(
        token=token,
        role=user_data["role"],
    )
