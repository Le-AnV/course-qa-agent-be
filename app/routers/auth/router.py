from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.auth import LoginRequest, LoginResponse
from app.services.auth import create_access_token, verify_credentials

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest, db: Session = Depends(get_db)):  # noqa: B008
    # 1. Xác thực tài khoản với database (hoặc fallback)
    user_data = verify_credentials(request.identifier, request.password, db=db)

    # 2. Tạo Token JWT
    token = create_access_token(data={"sub": user_data["identifier"], "role": user_data["role"]})

    # 3. Trả về Frontend
    return LoginResponse(token=token, role=user_data["role"])
