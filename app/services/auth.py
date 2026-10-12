from datetime import UTC, datetime, timedelta

import jwt
from fastapi import HTTPException, status

SECRET_KEY = "tam-thoi-dung-khoa-nay-cho-dev"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

MOCK_USERS = {
    "student@academic.edu.vn": {"password": "Student@123", "role": "student"},
    "admin@academic.edu.vn": {"password": "Admin@123", "role": "admin"},
    "teacher@academic.edu.vn": {"password": "Teacher@123", "role": "trainer"}
}

def verify_credentials(identifier: str, password: str) -> dict:
    normalized = identifier.strip().lower()
    if not normalized.endswith("@academic.edu.vn"):
        normalized += "@academic.edu.vn"
        
    user = MOCK_USERS.get(normalized)
    
    if not user or user["password"] != password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password."
        )
    return {"identifier": normalized, "role": user["role"]}

def create_access_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(UTC) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
