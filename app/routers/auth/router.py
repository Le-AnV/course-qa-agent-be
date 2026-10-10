from fastapi import APIRouter
from app.schemas.auth import LoginRequest, LoginResponse
from app.services.auth import verify_credentials, create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest):
    # 1. Kiểm tra tài khoản giả lập
    user_data = verify_credentials(request.identifier, request.password)
    
    # 2. Tạo Token JWT
    token = create_access_token(data={"sub": user_data["identifier"], "role": user_data["role"]})
    
    # 3. Trả về Frontend
    return LoginResponse(
        token=token,
        role=user_data["role"]
    )
