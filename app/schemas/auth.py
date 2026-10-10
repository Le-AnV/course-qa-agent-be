from pydantic import BaseModel


class LoginRequest(BaseModel):
    identifier: str
    password: str

class LoginResponse(BaseModel):
    token: str
    role: str
